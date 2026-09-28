from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import assert_same_org, require_client
from ..escalation import is_litigation_candidate
from ..letters.generator import render_letter
from ..secondary_bureaus import SECONDARY_BUREAUS

router = APIRouter(prefix="/reports/{report_id}/letters", tags=["letters"])

_BUREAU_RECIPIENTS = {
    "fcra_611": ("Equifax / Experian / TransUnion", "confirm current bureau dispute address before mailing"),
    "fcra_623_direct": ("the reporting creditor", "use the creditor's own dispute address"),
    "fdcpa_809": ("the collection agency", "use the collector's own address, from their first contact letter"),
    "fcra_609": ("Equifax / Experian / TransUnion", "confirm current bureau disclosure address before mailing"),
    "fcra_605b": ("Equifax / Experian / TransUnion", "confirm current bureau fraud/ID theft address before mailing"),
    "fcra_mov": ("the same bureau that reported the item verified", "reuse the original dispute's recipient"),
    "tila_1666b": ("the reporting creditor", "use the creditor's own billing-inquiries/dispute address"),
    "fcra_charge_off_1099c": ("Equifax / Experian / TransUnion", "confirm current bureau dispute address before mailing"),
}

_VALID_OUTCOME_STATUSES = {"deleted", "updated", "verified", "no_response"}

# Not a citation to give a specific consumer -- a triage note pointing to why a
# flagged case is worth a real attorney's look. See the strategy doc's
# "Litigation referral" section: this whole feature needs attorney sign-off,
# including state bar rules on any referral-fee arrangement, before it goes live.
_STATUTORY_NOTE = (
    "Willful FCRA violations carry statutory damages of $100-$1,000 per violation plus "
    "punitive damages and attorney's fees (15 U.S.C. §1681n); negligent violations allow "
    "actual damages plus fees (§1681o). This is a triage flag for attorney review, not a "
    "legal conclusion or legal advice."
)


def _get_owned_paid_report(db: Session, report_id: str, client: models.Client) -> models.Report:
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    assert_same_org(client, report.organization_id)
    if report.client_id != client.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your report")
    if not report.is_paid:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Full dispute letters unlock after checkout for this report -- see /billing/checkout/{report_id}",
        )
    return report


@router.post("/generate", response_model=list[schemas.DisputeLetterOut], status_code=status.HTTP_201_CREATED)
def generate_letters(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Generates one letter per violation that doesn't already have one. Paywall
    is enforced here: this endpoint requires report.is_paid, and per CROA the
    fee should map to work delivered at payment time -- this is that delivery."""
    report = _get_owned_paid_report(db, report_id, client)

    violations = (
        db.query(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )

    created_letters = []
    for violation in violations:
        already_has_letter = (
            db.query(models.DisputeLetter).filter(models.DisputeLetter.violation_id == violation.id).first()
        )
        if already_has_letter:
            continue

        tradeline = violation.tradeline
        recipient_name, recipient_note = _BUREAU_RECIPIENTS.get(
            violation.letter_type, ("recipient", "confirm current address before mailing")
        )

        body = render_letter(
            letter_type=violation.letter_type,
            client_name=client.full_name or client.email,
            client_address="(client mailing address on file)",
            creditor_name=tradeline.creditor_name or "Unknown creditor",
            finding_description=violation.description,
            recipient_name=recipient_name,
            recipient_address=recipient_note,
            account_number_last4=tradeline.account_number_last4,
            original_creditor_name=tradeline.original_creditor_name,
        )

        letter = models.DisputeLetter(
            violation_id=violation.id,
            letter_type=violation.letter_type,
            recipient=recipient_name,
            body_text=body,
        )
        db.add(letter)
        created_letters.append(letter)

    db.commit()
    for letter in created_letters:
        db.refresh(letter)
    return created_letters


@router.get("", response_model=list[schemas.DisputeLetterOut])
def list_letters(report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    report = _get_owned_paid_report(db, report_id, client)
    return (
        db.query(models.DisputeLetter)
        .join(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )


@router.post("/secondary-bureau-sweep", response_model=list[schemas.DisputeLetterOut])
def generate_secondary_bureau_letters(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """The 'close out/dispute the other bureaus too' feature: generates a
    disclosure-and-dispute letter to each specialty consumer reporting agency,
    not tied to a specific detected violation since the point is to check
    files most consumers never look at."""
    report = _get_owned_paid_report(db, report_id, client)

    # Not tied to a single Violation row (the point is to check files most
    # consumers never look at, not to react to a specific detected error), so
    # these are built and returned directly rather than persisted through the
    # DisputeLetter table. In production, give this its own table instead of
    # forcing it through a schema built around per-violation letters.
    results = []
    for i, agency in enumerate(SECONDARY_BUREAUS):
        body = render_letter(
            letter_type="secondary_bureau",
            client_name=client.full_name or client.email,
            client_address="(client mailing address on file)",
            creditor_name="",
            finding_description="",
            recipient_name=agency["name"],
            recipient_address=agency["mailing_address_placeholder"],
        )
        results.append(
            schemas.DisputeLetterOut(
                id=f"secondary-{i}",
                letter_type="secondary_bureau",
                recipient=agency["name"],
                body_text=body,
                generated_at=report.created_at,
            )
        )
    return results


def _get_owned_letter(db: Session, report_id: str, letter_id: str, client: models.Client) -> models.DisputeLetter:
    report = _get_owned_paid_report(db, report_id, client)
    letter = (
        db.query(models.DisputeLetter)
        .join(models.Violation)
        .join(models.Tradeline)
        .filter(models.DisputeLetter.id == letter_id, models.Tradeline.report_id == report.id)
        .first()
    )
    if not letter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Letter not found on this report")
    return letter


@router.post("/{letter_id}/outcome", response_model=schemas.DisputeLetterOut)
def record_outcome(
    report_id: str,
    letter_id: str,
    payload: schemas.OutcomeIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Closes the loop the strategy doc calls the single biggest missing piece:
    without this, nobody -- including the rules engine -- ever learns whether
    a dispute actually worked."""
    if payload.outcome_status not in _VALID_OUTCOME_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"outcome_status must be one of {sorted(_VALID_OUTCOME_STATUSES)}",
        )
    letter = _get_owned_letter(db, report_id, letter_id, client)
    letter.outcome_status = payload.outcome_status
    letter.outcome_notes = payload.outcome_notes
    letter.outcome_reported_at = datetime.utcnow()
    db.commit()
    db.refresh(letter)
    return letter


@router.post("/{letter_id}/escalate-mov", response_model=schemas.DisputeLetterOut, status_code=status.HTTP_201_CREATED)
def escalate_to_mov(
    report_id: str,
    letter_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Generates the Method of Verification follow-up -- the honest next step
    when a dispute comes back 'verified' rather than corrected, instead of
    leaving the consumer with silence."""
    original = _get_owned_letter(db, report_id, letter_id, client)
    if original.outcome_status != "verified":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A Method of Verification request only makes sense after the original dispute "
            "was reported back as 'verified' -- record that outcome first.",
        )
    if original.letter_type not in ("fcra_611", "fcra_623_direct"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MOV escalation applies to FCRA §611/§623 disputes.")

    violation = original.violation
    tradeline = violation.tradeline

    body = render_letter(
        letter_type="fcra_mov",
        client_name=client.full_name or client.email,
        client_address="(client mailing address on file)",
        creditor_name=tradeline.creditor_name or "Unknown creditor",
        finding_description=violation.description,
        recipient_name=original.recipient,
        recipient_address="reuse the original dispute's recipient address",
        account_number_last4=tradeline.account_number_last4,
        original_dispute_date=original.generated_at.date().isoformat(),
    )
    mov_letter = models.DisputeLetter(
        violation_id=violation.id,
        letter_type="fcra_mov",
        recipient=original.recipient,
        body_text=body,
        escalates_letter_id=original.id,
    )
    db.add(mov_letter)
    db.commit()
    db.refresh(mov_letter)
    return mov_letter


@router.post("/identity-theft-block/{tradeline_id}", response_model=schemas.DisputeLetterOut, status_code=status.HTTP_201_CREATED)
def generate_identity_theft_block(
    report_id: str,
    tradeline_id: str,
    payload: schemas.IdentityTheftBlockIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """FCRA §605B: forces a block within 4 business days, no furnisher
    investigation -- dramatically faster than a standard dispute, but only
    for items the client attests resulted from identity theft. We never
    guess at this algorithmically; it's the client's own statement, backed
    by their FTC identity theft report, exactly as the statute requires."""
    report = _get_owned_paid_report(db, report_id, client)
    tradeline = (
        db.query(models.Tradeline)
        .filter(models.Tradeline.id == tradeline_id, models.Tradeline.report_id == report.id)
        .first()
    )
    if not tradeline:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tradeline not found on this report")

    tradeline.flagged_identity_theft = True
    tradeline.identity_theft_report_number = payload.identity_theft_report_number

    body = render_letter(
        letter_type="fcra_605b",
        client_name=client.full_name or client.email,
        client_address="(client mailing address on file)",
        creditor_name=tradeline.creditor_name or "Unknown creditor",
        finding_description="",
        recipient_name="Equifax / Experian / TransUnion",
        recipient_address="confirm current bureau fraud/ID theft address before mailing",
        account_number_last4=tradeline.account_number_last4,
        identity_theft_report_number=payload.identity_theft_report_number,
    )

    # Not tied to a Violation row -- an identity-theft block is the client's own
    # attestation, not a rules-engine finding. We create a lightweight Violation
    # row anyway so the letter has a normal place to attach in this schema;
    # production would give identity-theft blocks their own table instead.
    placeholder_violation = models.Violation(
        tradeline_id=tradeline.id,
        rule_id="client_flagged_identity_theft",
        legal_basis="FCRA 605B (15 U.S.C. 1681c-2)",
        severity="high",
        description=(
            f"Client attests this tradeline does not relate to any transaction they made or "
            f"authorized. FTC identity theft report #{payload.identity_theft_report_number}."
        ),
        letter_type="fcra_605b",
    )
    db.add(placeholder_violation)
    db.flush()

    letter = models.DisputeLetter(
        violation_id=placeholder_violation.id,
        letter_type="fcra_605b",
        recipient="Equifax / Experian / TransUnion",
        body_text=body,
    )
    db.add(letter)
    db.commit()
    db.refresh(letter)
    return letter


@router.post("/late-payment-dispute/{tradeline_id}", response_model=schemas.DisputeLetterOut, status_code=status.HTTP_201_CREATED)
def generate_late_payment_dispute(
    report_id: str,
    tradeline_id: str,
    payload: schemas.LatePaymentDisputeIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """TILA Section 1666b: a narrow, fact-specific dispute that only fits when
    the creditor's own statement-mailing timing or payment-crediting was at
    fault -- not a general 'remove all late payments' tool. A credit report
    alone can't reveal statement-mailing dates, so this is the client's own
    attested account, same pattern as the identity-theft block below."""
    report = _get_owned_paid_report(db, report_id, client)
    tradeline = (
        db.query(models.Tradeline)
        .filter(models.Tradeline.id == tradeline_id, models.Tradeline.report_id == report.id)
        .first()
    )
    if not tradeline:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tradeline not found on this report")

    description = (
        f"Client states they made the payment dated {payload.payment_date} on time and disputes "
        f"the late mark tied to it under TILA 15 U.S.C. Section 1666b."
    )
    if payload.dispute_notes:
        description += f" Client notes: {payload.dispute_notes}"

    recipient_name, recipient_note = _BUREAU_RECIPIENTS["tila_1666b"]
    body = render_letter(
        letter_type="tila_1666b",
        client_name=client.full_name or client.email,
        client_address="(client mailing address on file)",
        creditor_name=tradeline.creditor_name or "Unknown creditor",
        finding_description=description,
        recipient_name=recipient_name,
        recipient_address=recipient_note,
        account_number_last4=tradeline.account_number_last4,
        payment_date=payload.payment_date,
    )

    # Not a rules-engine finding -- the client's own attestation about payment
    # timing, same reasoning as the identity-theft block's placeholder Violation.
    placeholder_violation = models.Violation(
        tradeline_id=tradeline.id,
        rule_id="client_disputed_late_payment_timing",
        legal_basis="TILA Section 1666b (15 U.S.C. Section 1666b)",
        severity="medium",
        description=description,
        letter_type="tila_1666b",
    )
    db.add(placeholder_violation)
    db.flush()

    letter = models.DisputeLetter(
        violation_id=placeholder_violation.id,
        letter_type="tila_1666b",
        recipient=recipient_name,
        body_text=body,
    )
    db.add(letter)
    db.commit()
    db.refresh(letter)
    return letter


@router.post("/charge-off-1099c-evidence/{tradeline_id}", response_model=schemas.DisputeLetterOut, status_code=status.HTTP_201_CREATED)
def generate_charge_off_1099c_dispute(
    report_id: str,
    tradeline_id: str,
    payload: schemas.ChargeOffEvidenceIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Uses a client-obtained Form 1099-C (directly, or via their own IRS Form
    4506-T request) as SUPPORTING EVIDENCE inside a normal FCRA Section
    611/623 accuracy dispute. This platform does not assert that a 1099-C
    automatically extinguishes a debt or forces deletion -- that theory is
    legally disputed; see fcra_charge_off_1099c_dispute.txt.jinja for exactly
    how the letter is worded."""
    report = _get_owned_paid_report(db, report_id, client)
    tradeline = (
        db.query(models.Tradeline)
        .filter(models.Tradeline.id == tradeline_id, models.Tradeline.report_id == report.id)
        .first()
    )
    if not tradeline:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tradeline not found on this report")

    cancelled_amount_str = None
    if payload.cancelled_amount_cents is not None:
        cancelled_amount_str = f"${payload.cancelled_amount_cents / 100:,.2f}"

    description = (
        f"Client obtained a Form 1099-C for tax year {payload.tax_year}"
        + (f" showing {cancelled_amount_str} cancelled" if cancelled_amount_str else "")
        + " on this charged-off account, and disputes the balance/status currently being reported "
        "as inconsistent with that filing."
    )
    if payload.notes:
        description += f" Client notes: {payload.notes}"

    recipient_name, recipient_note = _BUREAU_RECIPIENTS["fcra_charge_off_1099c"]
    body = render_letter(
        letter_type="fcra_charge_off_1099c",
        client_name=client.full_name or client.email,
        client_address="(client mailing address on file)",
        creditor_name=tradeline.creditor_name or "Unknown creditor",
        finding_description=description,
        recipient_name=recipient_name,
        recipient_address=recipient_note,
        account_number_last4=tradeline.account_number_last4,
        original_creditor_name=tradeline.original_creditor_name,
        tax_year=payload.tax_year,
        cancelled_amount=cancelled_amount_str,
    )

    placeholder_violation = models.Violation(
        tradeline_id=tradeline.id,
        rule_id="client_charge_off_1099c_evidence",
        legal_basis=(
            "FCRA Sections 611 and 623 (15 U.S.C. 1681i, 1681s-2) -- 1099-C used as supporting "
            "evidence of a reporting inconsistency, not a standalone deletion theory"
        ),
        severity="medium",
        description=description,
        letter_type="fcra_charge_off_1099c",
    )
    db.add(placeholder_violation)
    db.flush()

    letter = models.DisputeLetter(
        violation_id=placeholder_violation.id,
        letter_type="fcra_charge_off_1099c",
        recipient=recipient_name,
        body_text=body,
    )
    db.add(letter)
    db.commit()
    db.refresh(letter)
    return letter


@router.get("/litigation-candidates", response_model=list[schemas.LitigationCandidateOut])
def get_litigation_candidates(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    """Surfaces violations where the outcome history looks worth a real
    attorney's review -- see app/escalation.py for the exact, deterministic
    criteria. Never a legal conclusion on its own."""
    report = _get_owned_paid_report(db, report_id, client)
    violations = (
        db.query(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )

    candidates = []
    for violation in violations:
        letters = (
            db.query(models.DisputeLetter).filter(models.DisputeLetter.violation_id == violation.id).all()
        )
        if not letters:
            continue
        flag = is_litigation_candidate(letters)
        if flag:
            candidates.append(
                schemas.LitigationCandidateOut(
                    violation_id=violation.id,
                    rule_id=violation.rule_id,
                    creditor_name=violation.tradeline.creditor_name,
                    reason=flag["reason"],
                    detail=flag["detail"],
                    statutory_note=_STATUTORY_NOTE,
                )
            )
    return candidates
