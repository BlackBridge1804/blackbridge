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
   Only active while AGREEMENT_REQUIRED is false, because the signed agreement
   promises a 3-business-day cancellation right that this rule would contradict.

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
    "I understand that my full breakdown and letters are delivered digitally the moment "
    "I unlock them, so my purchase is non-refundable once unlocked. The only exception is "
    "if BlackBridge was unable to read my credit report. Results are not guaranteed. "
    "I have read the Refund Policy."
)


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


def refund_final_active() -> bool:
    return bool(settings.refund_final_after_unlock) and not settings.agreement_required


def free_days() -> int:
    return max(0, int(settings.free_scan_days or 0))


def public_policy() -> dict:
    return {
        "refund_final": refund_final_active(),
        "free_scan_days": free_days(),
        "accept_text": ACCEPT_TEXT,
        "version": POLICY_VERSION,
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


def record_acceptance(db: Session, report: models.Report, ip: str) -> None:
    db.add(RefundAcceptance(
        organization_id=report.organization_id,
        client_id=report.client_id,
        report_id=report.id,
        accepted_ip=(ip or "")[:64],
        version=POLICY_VERSION,
        text=ACCEPT_TEXT,
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
    if refund_final_active() and not has_acceptance(db, report):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This client has not ticked the no-refund-after-unlock box yet. Ask them to open "
            "their report, tick the box and press the Unlock button, then mark it paid.",
        )
