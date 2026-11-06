"""
Receipts for payments the business has recorded as received (report unlocks and Credit
Builder payment steps). A receipt is created when staff record the payment, and voided
if that payment is later undone. Receipts only ever show what staff recorded; nothing
here moves money.
"""
import re
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .database import Base, engine


def _uuid() -> str:
    return str(uuid.uuid4())


class Receipt(Base):
    __tablename__ = "receipts"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    number = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # "report" | "builder"
    source_id = Column(String, nullable=True, index=True)  # report id or builder milestone id
    description = Column(String, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    method = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)
    issued_by = Column(String, nullable=True)
    voided_at = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine, tables=[Receipt.__table__])


def to_cents(text) -> Optional[int]:
    """'49.99', '$1,000', '250' -> cents. None if it is not a positive amount."""
    s = re.sub(r"[\s$,]", "", str(text or ""))
    if not re.fullmatch(r"\d{1,7}(\.\d{1,2})?", s):
        return None
    cents = int(round(float(s) * 100))
    return cents if cents > 0 else None


def money(cents: int) -> str:
    return "${:,.2f}".format(cents / 100)


def issue(db: Session, org_id: str, client_id: str, kind: str, source_id: str, description: str,
          amount_cents: int, method: Optional[str] = None, reference: Optional[str] = None,
          issued_by: Optional[str] = None) -> Receipt:
    """One live receipt per payment: asking again for the same payment returns the existing one."""
    existing = db.query(Receipt).filter(Receipt.source_id == source_id, Receipt.kind == kind,
                                        Receipt.voided_at.is_(None)).first()
    if existing is not None:
        return existing
    n = db.query(Receipt).filter(Receipt.organization_id == org_id).count() + 1
    r = Receipt(organization_id=org_id, client_id=client_id, number=f"R-{n:05d}", kind=kind, source_id=source_id,
                description=description[:200], amount_cents=amount_cents, method=(method or "")[:40] or None,
                reference=(reference or "")[:120] or None, issued_by=issued_by)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def void_for(db: Session, kind: str, source_id: str) -> None:
    for r in db.query(Receipt).filter(Receipt.source_id == source_id, Receipt.kind == kind,
                                      Receipt.voided_at.is_(None)).all():
        r.voided_at = datetime.utcnow()
    db.commit()


def business_info(db: Session, org: models.Organization) -> dict:
    """Who the receipt is from. The main site uses the legal company name."""
    from .routers import org_settings as _os
    row = _os.get_row(db, org.id)
    if _os.is_platform_org(org):
        return {"name": settings.platform_legal_name, "address": settings.business_address.strip(),
                "phone": (row.phone if row and row.phone else "(669) 272-2854"),
                "email": (row.email if row and row.email else "BlackBridgeoffice@Gmail.com")}
    return {"name": _os.display_name_of(db, org), "address": (row.business_address if row else None) or "",
            "phone": (row.phone if row else None) or "", "email": (row.email if row else None) or ""}


def view(db: Session, r: Receipt) -> dict:
    org = db.query(models.Organization).filter(models.Organization.id == r.organization_id).first()
    c = db.query(models.Client).filter(models.Client.id == r.client_id).first()
    return {
        "id": r.id, "source_id": r.source_id, "kind": r.kind, "number": r.number, "description": r.description, "amount_cents": r.amount_cents,
        "amount_text": money(r.amount_cents), "method": r.method or "", "reference": r.reference or "",
        "paid_at": r.paid_at.isoformat() if r.paid_at else None, "void": bool(r.voided_at),
        "client_name": (c.full_name or c.email) if c else "", "client_email": c.email if c else "",
        "business": business_info(db, org) if org else {},
    }
