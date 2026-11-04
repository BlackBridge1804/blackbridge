"""
Two owner-controlled switches, both OFF until set in Render:

1. FREE_SCAN_DAYS (e.g. 3): the free summary of an unpaid report disappears this
   many days after the report was first uploaded. Paying brings everything back.
   The clock starts at the earliest upload of the same report text by the same
   client, so uploading the same file again does not restart it.

2. REFUND_FINAL_AFTER_UNLOCK=true: before paying, the client must tick a box
   saying the purchase is non-refundable once unlocked (except where the law
   requires a refund or BlackBridge fails to deliver). The tick is recorded with
   date, time and IP address. Staff cannot mark a report paid without it.
   It covers SELF-SERVICE clients only. Done-for-you clients are never put under
   it: with both switches on, they sign the service agreement (with its
   3-business-day cancellation right) and self-service clients tick the box.

Draft for attorney review. Not legal advice.
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .database import Base, engine

POLICY_VERSION = "2026-11-02-draft-1"

ACCEPT_TEXT = (
    "I understand that I can cancel at any time, but my full breakdown and letters are "
    "delivered digitally the moment I unlock them, so there is no refund once I have access. "
    "The only exception is if BlackBridge was unable to read my credit report. "
    "Results are not guaranteed. I have read the Refund Policy."
)


def accept_text_for(db: Session, client: models.Client) -> str:
    """The exact words this client ticks. The main site uses ACCEPT_TEXT; a
    licensee's clients see the licensee's name, or the licensee's own refund policy."""
    from .routers import org_settings as _os
    org = db.query(models.Organization).filter(models.Organization.id == client.organization_id).first()
    if org is None or _os.is_platform_org(org):
        return ACCEPT_TEXT
    name = _os.display_name_of(db, org)
    pol = _os.refund_policy_for(db, org)
    if pol["mode"] == "custom":
        return f"I have read and I agree to {name}'s refund policy: {pol['text']}"
    return ACCEPT_TEXT.replace("BlackBridge", name)


def _uuid() -> str:
    return str(uuid.uuid4())


class RefundAcceptance(Base):
    __tablename__ = "refund_acceptances"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    report_id = Column(String, nullable=False, index=True)
    accepted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    accepted_ip = Column(String, nullable=True)
    version = Column(String, nullable=False)
    text = Column(Text, nullable=False)


Base.metadata.create_all(bind=engine, tables=[RefundAcceptance.__table__])


def _service_type(db: Session, client_id: str) -> str:
    """"diy" (self-service) or "done_for_you". Anyone without a profile is diy."""
    try:
        from .routers.profile import ClientProfile
    except ImportError:
        return "diy"
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client_id).first()
    return (p.service_type if p and p.service_type else "diy")


def refund_final_for(db: Session, client_id: str) -> bool:
    """The no-refund-after-unlock rule covers self-service clients only.
    Done-for-you clients are never put under it; they sign the agreement."""
    return bool(settings.refund_final_after_unlock) and _service_type(db, client_id) != "done_for_you"


def agreement_applies(db: Session, client_id: str) -> bool:
    """Does this client have to sign the service agreement before unlocking?
    AGREEMENT_REQUIRED off: nobody. On: everybody, unless the no-refund rule is
    also on, in which case only done-for-you clients (self-service clients get
    the no-refund checkbox instead)."""
    if not settings.agreement_required:
        return False
    if settings.refund_final_after_unlock:
        return _service_type(db, client_id) == "done_for_you"
    return True


def free_days() -> int:
    return max(0, int(settings.free_scan_days or 0))


def public_policy() -> dict:
    return {
        "refund_final": bool(settings.refund_final_after_unlock),
        "free_scan_days": free_days(),
        "accept_text": ACCEPT_TEXT,
        "version": POLICY_VERSION,
        "price_report": settings.price_report,
        "price_bundle": settings.price_bundle,
    }


def pay_info(db: Session, client: models.Client) -> Optional[dict]:
    """How to pay by Zelle, shown only to a signed-in client. A licensee's
    clients see the LICENSEE's payment info, never BlackBridge's. None when no
    payment info is set up or a card processor is connected instead."""
    from .routers import org_settings as _os
    org = db.query(models.Organization).filter(models.Organization.id == client.organization_id).first()
    eff = _os.effective(db, org) if org else {}
    contact = (eff.get("zelle_contact") or "").strip()
    other = (eff.get("payment_instructions") or "").strip()
    if (not contact and not other) or (settings.stripe_secret_key and eff.get("platform")):
        return None
    name = (eff.get("zelle_name") or "").strip()
    note = (eff.get("zelle_note") or "").strip()
    if not note and eff.get("platform") and name and "blackbridge" not in name.lower():
        note = (
            f"Zelle will show the name {name}. {name} is a sister company of BlackBridge LLC "
            "and is our payment partner. It is the same business and the same service, "
            "just a different name on the payment."
        )
    email = client.email
    return {
        "method": "zelle",
        "contact": contact,
        "name": name,
        "note": note,
        "memo": email,
        "instructions": other,
        "price_report": eff.get("price_report") or settings.price_report,
        "price_bundle": eff.get("price_bundle") or settings.price_bundle,
    }


def free_window(db: Session, report: models.Report) -> dict:
    """Where this report's free preview stands. Paid reports never expire."""
    days = free_days()
    if report.is_paid or days <= 0:
        return {"expires_at": None, "expired": False}
    start = report.created_at or datetime.utcnow()
    if report.raw_text:
        earliest = (
            db.query(func.min(models.Report.created_at))
            .filter(models.Report.client_id == report.client_id, models.Report.raw_text == report.raw_text)
            .scalar()
        )
        if earliest and earliest < start:
            start = earliest
    expires = start + timedelta(days=days)
    return {"expires_at": expires, "expired": datetime.utcnow() >= expires}


def record_acceptance(db: Session, report: models.Report, ip: str, text: Optional[str] = None) -> None:
    db.add(RefundAcceptance(
        organization_id=report.organization_id,
        client_id=report.client_id,
        report_id=report.id,
        accepted_ip=(ip or "")[:64],
        version=POLICY_VERSION,
        text=text or ACCEPT_TEXT,
    ))
    db.commit()


def has_acceptance(db: Session, report: models.Report) -> bool:
    return (
        db.query(RefundAcceptance)
        .filter(RefundAcceptance.client_id == report.client_id, RefundAcceptance.report_id == report.id)
        .first()
        is not None
    )


def require_acceptance_for_staff(db: Session, report: models.Report) -> None:
    if refund_final_for(db, report.client_id) and not has_acceptance(db, report):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This client has not ticked the no-refund-after-unlock box yet. Ask them to open "
            "their report, tick the box and press the Unlock button, then mark it paid.",
        )
