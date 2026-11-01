"""
Guided identity-theft block flow (FCRA 605B).

What this adds on top of the existing one-account letter:
  * A saved FTC report number for the report (the client files the FTC
    report THEMSELVES at identitytheft.gov; this app never files anything).
  * A required confirmation that the selected accounts are not the client's,
    saved with the date, time and IP address (table idtheft_attestations).
  * One step to generate block letters for several accounts at once.
  * A status view that drives the checklist in the app.

Two new tables are created automatically on the next deploy (no manual
database step): idtheft_cases and idtheft_attestations.

Draft for attorney review. Not legal advice.
"""
import re
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Session

from .. import models
from ..database import Base, engine, get_db
from ..deps import require_client

ATTESTATION_VERSION = "2026-10-31-draft-1"

ATTESTATION_TEXT = (
    "I confirm that the account(s) I selected do not belong to me. I did not open them, "
    "and I did not make or authorize the transactions on them. The information in my "
    "FTC identity theft report is true. I understand that a credit bureau can remove a "
    "block that was obtained by a false statement, and that knowingly making a false "
    "identity theft claim is illegal and can hurt me."
)

# Mailed with each block letter. The statute asks for proof of identity, a copy
# of the identity theft report, and identification of the information.
ENCLOSURES = [
    "A copy of your FTC Identity Theft Report (the PDF you download from identitytheft.gov)",
    "A copy of your government-issued photo ID",
    "A copy of proof of your current address (a utility, bank or insurance statement)",
    "A copy of the credit report page showing the account, with the account marked",
]

# Calendar days to wait before treating silence as a missed deadline. The law
# says 4 business days after the bureau receives the request; we add time for
# the mail. The client confirms the real dates before filing any complaint.
WAIT_DAYS = 14

_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{2,38}[A-Za-z0-9]$")


def _uuid() -> str:
    return str(uuid.uuid4())


class IdTheftCase(Base):
    __tablename__ = "idtheft_cases"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    report_id = Column(String, ForeignKey("reports.id"), nullable=False, unique=True, index=True)
    report_number = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class IdTheftAttestation(Base):
    __tablename__ = "idtheft_attestations"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    report_id = Column(String, ForeignKey("reports.id"), nullable=False, index=True)
    tradeline_id = Column(String, ForeignKey("tradelines.id"), nullable=False, index=True)
    report_number = Column(String, nullable=False)
    text_version = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    confirmed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    confirmed_ip = Column(String, nullable=True)


Base.metadata.create_all(bind=engine, tables=[IdTheftCase.__table__, IdTheftAttestation.__table__])


# ---- helpers shared with the one-account endpoint in letters.py -----------
def clean_report_number(raw: Optional[str]) -> str:
    n = (raw or "").strip()
    if not _NUMBER_RE.match(n):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That doesn't look like an FTC report number. Copy it exactly from your report "
            "on identitytheft.gov (letters, numbers and dashes only).",
        )
    return n


def require_attestation(confirmed: bool) -> None:
    if not confirmed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Please confirm the statement that these accounts are not yours before generating a block letter.",
        )


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def create_605b_letter(
    db: Session,
    client: models.Client,
    report: models.Report,
    tradeline: models.Tradeline,
    number: str,
    ip: str,
) -> models.DisputeLetter:
    """Flags the account, saves the confirmation, and adds the block letter.
    Does NOT commit: the caller commits once."""
    from .letters import render_letter  # late import: letters.py mounts this module
    from .profile import require_mailing_address

    body = render_letter(
        letter_type="fcra_605b",
        client_name=client.full_name or client.email,
        client_address=require_mailing_address(db, client),
        creditor_name=tradeline.creditor_name or "Unknown creditor",
        finding_description="",
        recipient_name="Equifax / Experian / TransUnion",
        recipient_address="confirm current bureau fraud/ID theft address before mailing",
        account_number_last4=tradeline.account_number_last4,
        identity_theft_report_number=number,
    )

    tradeline.flagged_identity_theft = True
    tradeline.identity_theft_report_number = number

    # An identity-theft block is the client's own statement, not a rules-engine
    # finding. A lightweight Violation row gives the letter its normal place.
    placeholder = models.Violation(
        tradeline_id=tradeline.id,
        rule_id="client_flagged_identity_theft",
        legal_basis="FCRA 605B (15 U.S.C. 1681c-2)",
        severity="high",
        description=(
            "Client attests this tradeline does not relate to any transaction they made or "
            f"authorized. FTC identity theft report #{number}."
        ),
        letter_type="fcra_605b",
    )
    db.add(placeholder)
    db.flush()

    letter = models.DisputeLetter(
        violation_id=placeholder.id,
        letter_type="fcra_605b",
        recipient="Equifax / Experian / TransUnion",
        body_text=body,
    )
    db.add(letter)

    db.add(IdTheftAttestation(
        organization_id=report.organization_id,
        client_id=client.id,
        report_id=report.id,
        tradeline_id=tradeline.id,
        report_number=number,
        text_version=ATTESTATION_VERSION,
        text=ATTESTATION_TEXT,
        confirmed_at=datetime.utcnow(),
        confirmed_ip=ip,
    ))
    db.flush()
    return letter


def _block_letters_for_report(db: Session, report_id: str) -> dict:
    """tradeline_id -> newest 605B letter for it."""
    rows = (
        db.query(models.DisputeLetter, models.Tradeline)
        .join(models.Violation, models.DisputeLetter.violation_id == models.Violation.id)
        .join(models.Tradeline, models.Violation.tradeline_id == models.Tradeline.id)
        .filter(models.Tradeline.report_id == report_id, models.DisputeLetter.letter_type == "fcra_605b")
        .order_by(models.DisputeLetter.generated_at.asc())
        .all()
    )
    out = {}
    for letter, tl in rows:
        out[tl.id] = letter
    return out


def _case_for(db: Session, report_id: str) -> Optional[IdTheftCase]:
    return db.query(IdTheftCase).filter(IdTheftCase.report_id == report_id).first()


# ---- routes ----------------------------------------------------------------
router = APIRouter(tags=["identity-theft guide"])


class CaseIn(BaseModel):
    identity_theft_report_number: str


class GenerateIn(BaseModel):
    tradeline_ids: list[str] = Field(default_factory=list, max_length=50)
    confirm_not_mine: bool = False


def _owned_paid_report(db: Session, report_id: str, client: models.Client) -> models.Report:
    from .letters import _get_owned_paid_report

    return _get_owned_paid_report(db, report_id, client)


@router.get("/idtheft/status")
def idtheft_status(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    report = _owned_paid_report(db, report_id, client)
    case = _case_for(db, report.id)
    number = case.report_number if case else None
    tradelines = db.query(models.Tradeline).filter(models.Tradeline.report_id == report.id).all()
    letters = _block_letters_for_report(db, report.id)
    now = datetime.utcnow()

    accounts = []
    for t in tradelines:
        letter = letters.get(t.id)
        days = None
        if letter is not None and letter.generated_at is not None:
            days = max(0, (now - letter.generated_at).days)
        accounts.append({
            "tradeline_id": t.id,
            "creditor_name": t.creditor_name,
            "account_number_last4": t.account_number_last4,
            "has_letter": letter is not None,
            "letter_id": letter.id if letter else None,
            "letter_generated_at": letter.generated_at if letter else None,
            "outcome_status": letter.outcome_status if letter else None,
            "days_waiting": days,
            "past_window": bool(letter and not letter.outcome_status and days is not None and days >= WAIT_DAYS),
        })

    any_letter = bool(letters)
    all_answered = any_letter and all(l.outcome_status for l in letters.values())
    any_outcome = any(l.outcome_status for l in letters.values())
    steps = [
        {"key": "ftc_report", "label": "File your FTC identity theft report (you do this on identitytheft.gov)", "done": bool(number)},
        {"key": "generate", "label": "Pick the accounts, confirm, and create your block letters", "done": any_letter},
        {"key": "mail", "label": "Mail each letter with the enclosures (certified mail is best)", "done": any_outcome},
        {"key": "respond", "label": "Record each bureau's response", "done": all_answered},
    ]
    current = next((s["key"] for s in steps if not s["done"]), "complete")

    return {
        "report_id": report.id,
        "report_number": number,
        "steps": steps,
        "current_step": current,
        "accounts": accounts,
        "attestation_text": ATTESTATION_TEXT,
        "attestation_version": ATTESTATION_VERSION,
        "enclosures": ENCLOSURES,
        "wait_days": WAIT_DAYS,
        "ftc_url": "https://www.identitytheft.gov",
    }


@router.post("/idtheft/case")
def save_case(
    report_id: str,
    payload: CaseIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Saves the FTC report number the client got from identitytheft.gov."""
    report = _owned_paid_report(db, report_id, client)
    number = clean_report_number(payload.identity_theft_report_number)
    case = _case_for(db, report.id)
    if case is None:
        case = IdTheftCase(organization_id=report.organization_id, client_id=client.id, report_id=report.id)
        db.add(case)
    case.report_number = number
    case.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "report_number": number}


@router.post("/idtheft/generate", status_code=status.HTTP_201_CREATED)
def generate_blocks(
    report_id: str,
    payload: GenerateIn,
    request: Request,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """One block letter per selected account, using the saved report number."""
    report = _owned_paid_report(db, report_id, client)
    require_attestation(payload.confirm_not_mine)
    case = _case_for(db, report.id)
    if case is None or not case.report_number:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save your FTC identity theft report number first (step 1).",
        )
    ids = list(dict.fromkeys(payload.tradeline_ids))
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick at least one account.")

    tradelines = (
        db.query(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id, models.Tradeline.id.in_(ids))
        .all()
    )
    if len(tradelines) != len(ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One of those accounts isn't on this report.")

    existing = _block_letters_for_report(db, report.id)
    ip = client_ip(request)
    created, skipped = [], []
    for t in tradelines:
        if t.id in existing:
            skipped.append({"tradeline_id": t.id, "creditor_name": t.creditor_name, "reason": "already has a block letter"})
            continue
        letter = create_605b_letter(db, client, report, t, case.report_number, ip)
        created.append({"tradeline_id": t.id, "creditor_name": t.creditor_name, "letter_id": letter.id})
    db.commit()
    return {"created": created, "skipped": skipped}
