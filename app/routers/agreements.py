"""
Signed service agreement + consumer rights statement.

Flow for a client:
  1. Read and acknowledge the Consumer Credit File Rights statement
     (a separate document from the contract, as the law requires).
  2. Read and sign the Service Agreement (typed name + checkbox).
  3. A 3-business-day cancellation window starts. The client can cancel in
     the app during it. Paid services (unlock) wait until it ends, unless
     AGREEMENT_WAITING_PERIOD=false.

Everything here is switched off unless settings.agreement_required is True.
New table `client_agreements` is created automatically (no manual DB step).
This is a draft for attorney review, not legal advice.
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Session

from .. import agreement_text as T
from .. import models
from ..config import settings
from ..database import Base, engine, get_db
from ..deps import require_client, require_org_staff
from ..models import UserRole


def _uuid() -> str:
    return str(uuid.uuid4())


class ClientAgreement(Base):
    __tablename__ = "client_agreements"

    id = Column(String, primary_key=True, default=_uuid)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    version = Column(String, nullable=False)
    provider_name = Column(String, nullable=False)
    business_address = Column(String, nullable=False)
    waiting_period = Column(Boolean, default=True)

    # step 1: consumer rights statement
    disclosure_text = Column(Text, nullable=False)
    disclosure_signed_name = Column(String, nullable=False)
    disclosure_ack_at = Column(DateTime, nullable=False)

    # step 2: the contract
    agreement_text = Column(Text, nullable=True)
    signed_name = Column(String, nullable=True)
    signed_at = Column(DateTime, nullable=True)
    signed_ip = Column(String, nullable=True)
    signed_user_agent = Column(String, nullable=True)
    cancel_deadline = Column(DateTime, nullable=True)

    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine, tables=[ClientAgreement.__table__])


# ---- time helpers ---------------------------------------------------------
def _tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.agreement_timezone)
    except Exception:  # missing tz database: fall back to a fixed offset
        return timezone(timedelta(hours=-5))


def _now() -> datetime:
    """Naive UTC, matching the other tables."""
    return datetime.utcnow()


def compute_cancel_deadline(signed_at_utc: datetime) -> datetime:
    """Midnight at the end of the 3rd business day after signing, in the
    business's time zone, returned as naive UTC. Business days are Mon-Fri
    (federal holidays are NOT skipped -- confirm the rule with your attorney)."""
    tz = _tz()
    local = signed_at_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    d: date = local.date()
    counted = 0
    while counted < 3:
        d += timedelta(days=1)
        if d.weekday() < 5:
            counted += 1
    end_local = datetime.combine(d, time(23, 59, 59), tzinfo=tz)
    return end_local.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_local(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    local = dt.replace(tzinfo=timezone.utc).astimezone(_tz())
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a %b')} {local.day}, {local.year} at {hour}:{local.strftime('%M %p')} ET"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


# ---- core logic -----------------------------------------------------------
def provider_name_for(db: Session, organization_id: str) -> str:
    org = db.query(models.Organization).filter(models.Organization.id == organization_id).first()
    if not org:
        return settings.platform_legal_name
    if org.slug == settings.platform_org_slug:
        return settings.platform_legal_name
    return org.name


def address_for_org(db: Session, organization_id: str) -> str:
    """The principal business address printed in this organization's agreement.
    The main site uses BUSINESS_ADDRESS from Render; a licensee uses the address
    saved in their own Business settings (blank until they save one)."""
    org = db.query(models.Organization).filter(models.Organization.id == organization_id).first()
    if org and org.slug == settings.platform_org_slug:
        return settings.business_address.strip()
    if org:
        from . import org_settings as _os
        return _os.effective(db, org)["address"]
    return ""


def latest_agreement(db: Session, client_id: str) -> Optional[ClientAgreement]:
    return (
        db.query(ClientAgreement)
        .filter(ClientAgreement.client_id == client_id)
        .order_by(ClientAgreement.created_at.desc())
        .first()
    )


def status_of(a: Optional[ClientAgreement]) -> str:
    if a is None:
        return "none"
    if a.cancelled_at is not None:
        return "cancelled"
    if a.signed_at is not None:
        return "signed"
    return "disclosure"


def _view(a: Optional[ClientAgreement], configured: bool = True) -> dict:
    """Status fields common to the client and staff responses."""
    st = status_of(a)
    now = _now()
    out = {
        "required": bool(settings.agreement_required),
        "configured": configured,
        "status": st,
        "signed_at": None,
        "signed_name": None,
        "cancel_deadline": None,
        "cancel_deadline_text": None,
        "can_cancel": False,
        "window_over": False,
        "unlock_allowed": False,
        "cancelled_at": None,
    }
    if a is None:
        return out
    out["signed_at"] = _iso(a.signed_at)
    out["signed_name"] = a.signed_name
    out["cancelled_at"] = _iso(a.cancelled_at)
    if a.cancel_deadline:
        out["cancel_deadline"] = _iso(a.cancel_deadline)
        out["cancel_deadline_text"] = fmt_local(a.cancel_deadline)
    if st == "signed":
        over = now > a.cancel_deadline
        out["window_over"] = over
        out["can_cancel"] = not over
        out["unlock_allowed"] = (not a.waiting_period) or over
    return out


def ensure_unlock_allowed(db: Session, client_id: str, for_client: bool = False) -> None:
    """Raise if this client's report must not be unlocked yet. No-op while the
    agreement feature is switched off."""
    from .. import policy
    if not policy.agreement_applies(db, client_id):
        return
    a = latest_agreement(db, client_id)
    st = status_of(a)
    who = "You have" if for_client else "This client has"
    if st in ("none", "disclosure"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{who} not signed the service agreement yet."
            + (" Please read and sign it above first." if for_client else ""),
        )
    if st == "cancelled":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            ("You cancelled your agreement" if for_client else "This client cancelled their agreement")
            + f" on {fmt_local(a.cancelled_at)}. Paid services cannot be provided under a cancelled agreement.",
        )
    if a.waiting_period and _now() <= a.cancel_deadline:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"The 3-business-day cancellation period ends {fmt_local(a.cancel_deadline)}. "
            + ("Your full breakdown can be unlocked after that." if for_client else "Do not unlock this report before then."),
        )


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _documents(a: ClientAgreement) -> dict:
    deadline_text = fmt_local(a.cancel_deadline) if a.cancel_deadline else "the end of the 3rd business day after signing"
    return {
        "version": a.version,
        "provider_name": a.provider_name,
        "disclosure_title": T.DISCLOSURE_TITLE,
        "disclosure_text": a.disclosure_text,
        "disclosure_signed_name": a.disclosure_signed_name,
        "disclosure_ack_at": fmt_local(a.disclosure_ack_at),
        "agreement_title": T.agreement_title(),
        "agreement_text": a.agreement_text,
        "cancel_statement": T.CANCEL_STATEMENT,
        "cancellation_notice": T.cancellation_notice(a.provider_name, a.business_address, deadline_text),
        "signed_name": a.signed_name,
        "signed_at_text": fmt_local(a.signed_at) if a.signed_at else None,
    }


def _preview_documents(provider: str, address: str) -> dict:
    """What the client sees before signing (not stored until they sign)."""
    return {
        "version": T.VERSION,
        "provider_name": provider,
        "disclosure_title": T.DISCLOSURE_TITLE,
        "disclosure_text": T.DISCLOSURE_TEXT,
        "agreement_title": T.agreement_title(),
        "agreement_text": T.agreement_text(provider, address, settings.agreement_waiting_period),
        "cancel_statement": T.CANCEL_STATEMENT,
    }


# ---- schemas --------------------------------------------------------------
class NameIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)


class SignIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    accept: bool


# ---- client routes (mounted at /staff/agreement/...) -----------------------
router = APIRouter(prefix="/agreement", tags=["agreement"])


def _require_ready(db: Session, organization_id: str, client_id: Optional[str] = None) -> str:
    """Returns the business address to print, or raises if the agreement is off
    or this organization has no address set up yet."""
    if not settings.agreement_required:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The agreement step is not turned on.")
    if client_id is not None:
        from .. import policy
        if not policy.agreement_applies(db, client_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "The agreement step is not turned on for your account.")
    address = address_for_org(db, organization_id)
    if not address:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Our service agreement is being finalized. Please contact us to complete your purchase.",
        )
    return address


@router.get("/current")
def get_current(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    a = latest_agreement(db, client.id)
    address = address_for_org(db, client.organization_id)
    out = _view(a, configured=bool(address))
    from .. import policy
    out["required"] = policy.agreement_applies(db, client.id)
    if not out["required"] or not out["configured"]:
        return out
    if a is not None and out["status"] in ("signed", "cancelled"):
        out["documents"] = _documents(a)
    else:
        out["documents"] = _preview_documents(provider_name_for(db, client.organization_id), address)
        if a is not None and out["status"] == "disclosure":
            out["documents"]["disclosure_signed_name"] = a.disclosure_signed_name
    return out


@router.post("/disclosure")
def acknowledge_disclosure(
    payload: NameIn, db: Session = Depends(get_db), client: models.Client = Depends(require_client)
):
    address = _require_ready(db, client.organization_id, client.id)
    a = latest_agreement(db, client.id)
    st = status_of(a)
    if st in ("signed", "disclosure"):
        return get_current(db, client)
    provider = provider_name_for(db, client.organization_id)
    a = ClientAgreement(
        client_id=client.id,
        organization_id=client.organization_id,
        version=T.VERSION,
        provider_name=provider,
        business_address=address,
        waiting_period=bool(settings.agreement_waiting_period),
        disclosure_text=T.DISCLOSURE_TEXT,
        disclosure_signed_name=payload.full_name.strip(),
        disclosure_ack_at=_now(),
    )
    db.add(a)
    db.commit()
    return get_current(db, client)


@router.post("/sign")
def sign_agreement(
    payload: SignIn,
    request: Request,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    _require_ready(db, client.organization_id, client.id)
    if not payload.accept:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please check the box to agree before signing.")
    a = latest_agreement(db, client.id)
    st = status_of(a)
    if st == "signed":
        return get_current(db, client)
    if st != "disclosure":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please read and acknowledge the consumer rights statement first.")
    signed_at = _now()
    a.agreement_text = T.agreement_text(a.provider_name, a.business_address, a.waiting_period)
    a.signed_name = payload.full_name.strip()
    a.signed_at = signed_at
    a.signed_ip = _client_ip(request)
    a.signed_user_agent = (request.headers.get("user-agent", "") or "")[:300]
    a.cancel_deadline = compute_cancel_deadline(signed_at)
    db.commit()
    return get_current(db, client)


@router.post("/cancel")
def cancel_agreement(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    _require_ready(db, client.organization_id, client.id)
    a = latest_agreement(db, client.id)
    if status_of(a) != "signed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "There is no active agreement to cancel.")
    if _now() > a.cancel_deadline:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The cancellation period has ended. Please contact us at " + T.CONTACT_EMAIL + " or " + T.CONTACT_PHONE + ".",
        )
    a.cancelled_at = _now()
    # If any of this client's reports were already unlocked, lock them again:
    # a cancelled agreement means no paid services, and anything they paid is
    # refunded (the staff payments page flags this).
    for rep in db.query(models.Report).filter(models.Report.client_id == client.id, models.Report.is_paid == True).all():  # noqa: E712
        rep.is_paid = False
    db.commit()
    return get_current(db, client)


# ---- staff routes (mounted at /staff/agreements) ---------------------------
staff_router = APIRouter(tags=["staff-agreements"])


@staff_router.get("/agreements")
def staff_list_agreements(
    org_id: str = Query(default=None),
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    if principal.role == UserRole.platform_admin:
        if not org_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass ?org_id=<organization id>.")
        resolved = org_id
    else:
        resolved = principal.organization_id
    rows = (
        db.query(ClientAgreement)
        .filter(ClientAgreement.organization_id == resolved)
        .order_by(ClientAgreement.created_at.asc())
        .all()
    )
    latest = {}
    for r in rows:  # ascending, so the last one per client wins
        latest[r.client_id] = r
    items = []
    for client_id, a in latest.items():
        v = _view(a)
        items.append(
            {
                "client_id": client_id,
                **{
                    k: v[k]
                    for k in (
                        "status",
                        "signed_at",
                        "signed_name",
                        "cancel_deadline",
                        "cancel_deadline_text",
                        "can_cancel",
                        "window_over",
                        "unlock_allowed",
                        "cancelled_at",
                    )
                },
            }
        )
    return {"required": bool(settings.agreement_required), "items": items}
