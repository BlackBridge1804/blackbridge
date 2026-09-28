from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import assert_same_org, require_client
from ..escalation import is_litigation_candidate
from ..parsing.extractor import extract_from_text, extract_pdf_text, extract_with_llm
from ..progress import compute_report_progress
from ..rules.engine import scan_tradelines, summarize

router = APIRouter(prefix="/reports", tags=["reports"])

# Anything that isn't the demo's pipe-delimited .txt sample format needs the
# LLM extraction path (app/parsing/extractor.py's extract_with_llm) rather than
# the dependency-free regex parser. PDFs get text pulled out with pypdf first;
# everything else (docx, csv exports, a plain-text paste of a real bureau
# report, etc.) is decoded as best-effort text and handed to the same LLM path.
_PDF_EXTENSIONS = (".pdf",)
_DEMO_TEXT_EXTENSIONS = (".txt",)


def _get_owned_report(db: Session, report_id: str, client: models.Client) -> models.Report:
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    assert_same_org(client, report.organization_id)
    if report.client_id != client.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your report")
    return report


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_report(
    file: UploadFile,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Accepts a .txt export for the zero-config demo pipeline, OR a real PDF
    credit report export, OR basically any other file (docx, csv, a plain-text
    paste, etc.) -- those all get routed to extract_with_llm() at scan time.
    PDFs get their text pulled out here with pypdf so raw_text is always plain
    text by the time it's stored."""
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()

    is_pdf = filename.endswith(_PDF_EXTENSIONS) or content_type == "application/pdf"
    is_demo_text = filename.endswith(_DEMO_TEXT_EXTENSIONS) and not is_pdf

    if is_pdf:
        try:
            raw_text = extract_pdf_text(raw_bytes)
        except Exception as exc:  # pypdf raises a variety of exception types on malformed PDFs
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Couldn't read this PDF ({exc}). If it's a scanned image rather "
                "than a text-based PDF, it needs OCR first -- try exporting a "
                "text-based copy from the bureau's site instead.",
            ) from exc
        if not raw_text.strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "This PDF has no extractable text -- it's likely a scanned image, "
                "which needs OCR before this pipeline can read it.",
            )
        source_format = "pdf"
    else:
        raw_text = raw_bytes.decode("utf-8", errors="ignore")
        source_format = "text" if is_demo_text else "other"

    report = models.Report(
        organization_id=client.organization_id,
        client_id=client.id,
        original_filename=file.filename,
        raw_text=raw_text,
        status="uploaded",
        source_format=source_format,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return {"report_id": report.id, "status": report.status, "source_format": report.source_format}


def _extract_tradelines(report: models.Report) -> list[dict]:
    """Picks the right parser for how this report was uploaded, with a
    zero-config fallback: the demo .txt format is parsed with the free regex
    parser, and only reaches for the LLM (and therefore ANTHROPIC_API_KEY) when
    that heuristic parser comes back empty or the file wasn't demo-format text
    to begin with."""
    raw_text = report.raw_text or ""

    if report.source_format == "text":
        tradeline_dicts = extract_from_text(raw_text)
        if tradeline_dicts:
            return tradeline_dicts
        # Fall through to the LLM path below if nothing matched and a key is
        # configured -- e.g. a .txt export that isn't the demo pipe-delimited
        # layout.

    if not settings.anthropic_api_key:
        if report.source_format == "text":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Could not parse any tradelines from this file using the built-in "
                "demo parser. For real bureau exports (PDFs, other text layouts), "
                "set ANTHROPIC_API_KEY so the platform can read them with Claude.",
            )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This file needs AI-assisted parsing (it isn't the plain demo .txt "
            "format), but ANTHROPIC_API_KEY isn't configured on this deployment. "
            "Ask your platform admin to set it.",
        )

    try:
        return extract_with_llm(raw_text, settings.anthropic_api_key)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Couldn't extract tradelines from this file: {exc}",
        ) from exc


@router.post("/{report_id}/scan", response_model=schemas.ReportSummaryOut)
def scan_report(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Parses the uploaded text into tradelines, runs the rules engine, and
    persists both -- this is the free tier's entire output: counts and
    categories, no letter content."""
    report = _get_owned_report(db, report_id, client)

    tradeline_dicts = _extract_tradelines(report)
    if not tradeline_dicts:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Could not find any tradelines in this file. Double-check it's a "
            "credit report export and not a blank or unrelated document.",
        )

    tradeline_rows = []
    for t in tradeline_dicts:
        row = models.Tradeline(report_id=report.id, **t)
        db.add(row)
        tradeline_rows.append(row)
    db.flush()  # assigns ids without committing, so the rules engine can reference them

    findings = scan_tradelines(tradeline_rows)
    for finding in findings:
        db.add(
            models.Violation(
                tradeline_id=finding["_tradeline_id"],
                rule_id=finding["rule_id"],
                legal_basis=finding["legal_basis"],
                severity=finding["severity"],
                description=finding["description"],
                letter_type=finding["letter_type"],
            )
        )

    report.status = "scanned"
    db.commit()

    summary = summarize(findings)
    return schemas.ReportSummaryOut(
        report_id=report.id,
        status=report.status,
        tradeline_count=len(tradeline_rows),
        violation_count=summary["violation_count"],
        violations_by_type=summary["violations_by_type"],
        is_paid=report.is_paid,
    )


def _summarize_report(db: Session, report: models.Report) -> schemas.ReportSummaryOut:
    violations = (
        db.query(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )
    by_type: dict = {}
    for v in violations:
        by_type[v.rule_id] = by_type.get(v.rule_id, 0) + 1

    tradeline_count = db.query(models.Tradeline).filter(models.Tradeline.report_id == report.id).count()
    return schemas.ReportSummaryOut(
        report_id=report.id,
        status=report.status,
        tradeline_count=tradeline_count,
        violation_count=len(violations),
        violations_by_type=by_type,
        is_paid=report.is_paid,
    )


@router.get("/{report_id}/summary", response_model=schemas.ReportSummaryOut)
def get_summary(report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    report = _get_owned_report(db, report_id, client)
    return _summarize_report(db, report)


def load_progress_for_report(db: Session, report: models.Report) -> dict:
    """Shared by the client-facing /progress endpoint below and the operator
    console (app/routers/staff.py) so both always show the same picture."""
    tradeline_count = db.query(models.Tradeline).filter(models.Tradeline.report_id == report.id).count()
    violations = (
        db.query(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )
    letters = (
        db.query(models.DisputeLetter)
        .join(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )

    litigation_count = 0
    letters_by_violation: dict = {}
    for l in letters:
        letters_by_violation.setdefault(l.violation_id, []).append(l)
    for violation_id, violation_letters in letters_by_violation.items():
        if is_litigation_candidate(violation_letters):
            litigation_count += 1

    return compute_report_progress(
        report=report,
        tradeline_count=tradeline_count,
        violation_count=len(violations),
        letters=letters,
        litigation_candidate_count=litigation_count,
    )


@router.get("/{report_id}/progress", response_model=schemas.ReportProgressOut)
def get_progress(report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    """Where this report stands in the pipeline right now -- available at the
    free tier, same as the summary counts, since it reveals stages/counts
    only, never letter content or violation detail."""
    report = _get_owned_report(db, report_id, client)
    return load_progress_for_report(db, report)


@router.get("", response_model=list[schemas.ReportSummaryOut])
def list_reports(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    """Every report this client has uploaded, newest first -- free-tier shape
    (counts only) for each, same as /summary. This is what a dashboard lists
    before the client drills into any one report."""
    reports = (
        db.query(models.Report)
        .filter(models.Report.client_id == client.id)
        .order_by(models.Report.created_at.desc())
        .all()
    )
    return [_summarize_report(db, r) for r in reports]


@router.get("/{report_id}/tradelines", response_model=list[schemas.TradelineOut])
def list_tradelines(report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    """Paid tier: the actual parsed account data, not just counts -- also what
    the frontend needs to let a client pick an account for an identity-theft
    block."""
    report = _get_owned_report(db, report_id, client)
    if not report.is_paid:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Account-level detail unlocks after checkout for this report -- see /billing/checkout/{report_id}",
        )
    return db.query(models.Tradeline).filter(models.Tradeline.report_id == report.id).all()


@router.get("/{report_id}/violations", response_model=list[schemas.ViolationOut])
def list_violations(report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    """Paid tier: every finding with its legal basis and description, each
    traceable to the rule that fired -- this is the 'detailed breakdown' the
    free tier's counts-only summary is paywalled in front of."""
    report = _get_owned_report(db, report_id, client)
    if not report.is_paid:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "The detailed violation breakdown unlocks after checkout for this report -- see /billing/checkout/{report_id}",
        )
    return (
        db.query(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )
