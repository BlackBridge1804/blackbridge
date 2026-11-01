"""
Limited authorization for do-it-for-you clients.

A client who is on the do-it-for-you service can sign a narrow, revocable
authorization that lets the provider's staff submit CFPB complaints in the
client's name. This is NOT a power of attorney. The CFPB lets credit repair
companies file as an authorized representative and expects written consent.

Rules built in:
  * Only clients marked "done_for_you" can sign.
  * Staff can see complaint drafts or log a filing only while the
    authorization is active (the client can cancel it any time, in the app).
  * If the signed-agreement flow is switched on, the client's agreement must
    also be signed and not cancelled.
  * Every staff view and every filing is written to authorization_events, and
    the client sees the filings in their own account.
  * It does not cover the FTC identity theft report. The client files that.

New tables (created automatically): client_authorizations, authorization_events.
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
from ..config import settings
from ..database import Base, engine, get_db
from ..deps import assert_same_org, require_client, require_org_staff
from . import cfpb_draft
from .profile import ClientProfile

AUTH_VERSION = "2026-10-31-draft-1"
TITLE = "Limited Authorization to File CFPB Complaints"


def _uuid() -> str:
    return str(uuid.uuid4())


class ClientAuthorization(Base):
    __tablename__ = "client_authorizations"

    id = Column(String, primary_key=True, default=_uuid)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    version = Column(String, nullable=False)
    provider_name = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    signed_name = Column(String, nullable=False)
    signed_at = Column(DateTime, nullable=False)
    signed_ip = Column(String, nullable=True)
    signed_user_agent = Column(String, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoked_ip = Column(String, nullable=True)


class AuthorizationEvent(Base):
    __tablename__ = "authorization_events"

    id = Column(String, primary_key=True, default=_uuid)
    authorization_id = Column(String, ForeignKey("client_authorizations.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    staff_user_id = Column(String, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)  # viewed_drafts | filed
    report_id = Column(String, nullable=True)
    company = Column(String, nullable=True)
    complaint_number = Column(String, nullable=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False)


Base.metadata.create_all(bind=engine, tables=[ClientAuthorization.__table__, AuthorizationEvent.__table__])


# ---- helpers ----------------------------------------------------------------
def provider_name(db: Session, organization_id: str) -> str:
    org = db.query(models.Organization).filter(models.Organization.id == organization_id).first()
    if org is None:
        return getattr(settings, "platform_legal_name", "BlackBridge LLC")
    if org.slug == getattr(settings, "platform_org_slug", "blackbridge-office-329"):
        return getattr(settings, "platform_legal_name", "BlackBridge LLC")
    return org.name


def authorization_text(provider: str) -> str:
    return (
        f"{TITLE}\n\n"
        f"I authorize {provider} and the staff who work for it (the \"Provider\") to submit complaints "
        "to the Consumer Financial Protection Bureau (CFPB), in my name and as my authorized "
        "representative, about my own credit reports. The complaints may name the credit reporting "
        "companies, creditors and collectors involved in the disputes I have started through this app.\n\n"
        "What the Provider may do:\n"
        "- Prepare and submit CFPB complaints for me.\n"
        "- Give the CFPB and the company my name, mailing address and the account details needed to identify my accounts.\n"
        "- Attach a copy of this signed authorization if a company asks for proof of my permission.\n\n"
        "What the Provider may NOT do under this authorization:\n"
        "- Sign contracts or agreements for me, or accept any offer on my behalf.\n"
        "- Receive, move or spend my money.\n"
        "- Open, close or change any account, or apply for credit in my name.\n"
        "- File my FTC identity theft report. I file that myself.\n"
        "- Ask for or use my passwords or full Social Security number.\n\n"
        "What I understand:\n"
        "- This is not a power of attorney. It covers only the CFPB complaints described above.\n"
        "- Each complaint filed for me will be listed in my account in this app with its date and company.\n"
        "- The CFPB sends the company's response to me, not to the Provider.\n"
        "- I can cancel this authorization at any time with the Cancel button in this app. It ends "
        "immediately. Complaints already filed stay filed.\n"
        "- Signing this is my choice. I can file my own CFPB complaints without it.\n"
        "- I will tell the Provider about anything that changes my mind or my facts."
    )


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def service_type_of(db: Session, client_id: str) -> str:
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client_id).first()
    return (p.service_type if p and p.service_type else "diy")


def latest_auth(db: Session, client_id: str) -> Optional[ClientAuthorization]:
    return (
        db.query(ClientAuthorization)
        .filter(ClientAuthorization.client_id == client_id)
        .order_by(ClientAuthorization.signed_at.desc())
        .first()
    )


def active_auth(db: Session, client_id: str) -> Optional[ClientAuthorization]:
    a = latest_auth(db, client_id)
    return a if a is not None and a.revoked_at is None else None


def _agreement_ok(db: Session, client_id: str) -> bool:
    """When the signed-agreement flow is on, the client's agreement must be signed
    and not cancelled. If that module isn't deployed, nothing to check."""
    if not getattr(settings, "agreement_required", False):
        return True
    try:
        from .agreements import latest_agreement, status_of
    except ImportError:
        return True
    return status_of(latest_agreement(db, client_id)) == "signed"


def _filings(db: Session, client_id: str) -> list:
    rows = (
        db.query(AuthorizationEvent)
        .filter(AuthorizationEvent.client_id == client_id, AuthorizationEvent.action == "filed")
        .order_by(AuthorizationEvent.at.desc())
        .all()
    )
    return [{"company": r.company, "report_id": r.report_id, "complaint_number": r.complaint_number, "filed_at": r.at} for r in rows]


def _view(db: Session, client: models.Client) -> dict:
    a = latest_auth(db, client.id)
    st = "none" if a is None else ("revoked" if a.revoked_at else "active")
    return {
        "title": TITLE,
        "version": AUTH_VERSION,
        "eligible": service_type_of(db, client.id) == "done_for_you",
        "status": st,
        "text": authorization_text(provider_name(db, client.organization_id)),
        "signed_name": a.signed_name if a else None,
        "signed_at": a.signed_at if a else None,
        "revoked_at": a.revoked_at if a else None,
        "filings": _filings(db, client.id),
    }


# ---- client routes -----------------------------------------------------------
router = APIRouter(prefix="/authorization", tags=["authorization"])


class SignIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    accept: bool = False


@router.get("/current")
def current(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    return _view(db, client)


@router.post("/sign", status_code=status.HTTP_201_CREATED)
def sign(
    payload: SignIn,
    request: Request,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    if service_type_of(db, client.id) != "done_for_you":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This authorization is only for do-it-for-you clients.")
    if active_auth(db, client.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have an active authorization.")
    if not payload.accept:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please tick the box to agree.")
    if client.full_name and _norm(payload.full_name) != _norm(client.full_name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Please type your name exactly as it appears on your account: {client.full_name}",
        )
    provider = provider_name(db, client.organization_id)
    db.add(ClientAuthorization(
        client_id=client.id,
        organization_id=client.organization_id,
        version=AUTH_VERSION,
        provider_name=provider,
        text=authorization_text(provider),
        signed_name=payload.full_name.strip(),
        signed_at=datetime.utcnow(),
        signed_ip=_ip(request),
        signed_user_agent=(request.headers.get("user-agent") or "")[:300],
    ))
    db.commit()
    return _view(db, client)


@router.post("/revoke")
def revoke(request: Request, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    a = active_auth(db, client.id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You don't have an active authorization.")
    a.revoked_at = datetime.utcnow()
    a.revoked_ip = _ip(request)
    db.commit()
    return _view(db, client)


# ---- staff routes ------------------------------------------------------------
class FiledIn(BaseModel):
    report_id: str
    company: str = Field(min_length=1, max_length=160)
    complaint_number: Optional[str] = Field(default=None, max_length=60)


def _client_for_staff(db: Session, client_id: str, principal: models.User) -> models.Client:
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    assert_same_org(principal, client.organization_id)
    return client


def _require_ready(db: Session, client: models.Client) -> ClientAuthorization:
    if service_type_of(db, client.id) != "done_for_you":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This client is not on the do-it-for-you service.")
    a = active_auth(db, client.id)
    if a is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No active authorization from this client. Do not file anything for them.")
    if not _agreement_ok(db, client.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This client has no signed service agreement on file. Do not file anything for them.")
    return a


@router.get("/staff/client/{client_id}")
def staff_status(client_id: str, db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    client = _client_for_staff(db, client_id, principal)
    v = _view(db, client)
    v.pop("text", None)
    v["service_type"] = service_type_of(db, client.id)
    v["agreement_ok"] = _agreement_ok(db, client.id)
    return v


@router.get("/staff/client/{client_id}/cfpb-drafts")
def staff_drafts(client_id: str, db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    client = _client_for_staff(db, client_id, principal)
    auth = _require_ready(db, client)
    reports = (
        db.query(models.Report)
        .filter(models.Report.client_id == client.id, models.Report.is_paid == True)  # noqa: E712
        .order_by(models.Report.created_at.desc())
        .all()
    )
    out = []
    for r in reports:
        d = cfpb_draft.build_draft(db, r)
        d["report_label"] = r.id[:8]
        out.append(d)
    db.add(AuthorizationEvent(
        authorization_id=auth.id, client_id=client.id, organization_id=client.organization_id,
        staff_user_id=principal.id, action="viewed_drafts",
    ))
    db.commit()
    return {"reports": out, "authorization_signed_at": auth.signed_at, "signed_name": auth.signed_name}


@router.post("/staff/client/{client_id}/filed", status_code=status.HTTP_201_CREATED)
def staff_filed(
    client_id: str,
    payload: FiledIn,
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    client = _client_for_staff(db, client_id, principal)
    auth = _require_ready(db, client)
    report = db.query(models.Report).filter(models.Report.id == payload.report_id, models.Report.client_id == client.id).first()
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That report isn't this client's.")
    db.add(AuthorizationEvent(
        authorization_id=auth.id, client_id=client.id, organization_id=client.organization_id,
        staff_user_id=principal.id, action="filed", report_id=report.id,
        company=payload.company.strip(),
        complaint_number=(payload.complaint_number or "").strip() or None,
    ))
    if report.cfpb_complaint_filed_at is None:
        report.cfpb_complaint_filed_at = datetime.utcnow()
        report.cfpb_complaint_outcome = "in_progress"
    db.commit()
    return {"ok": True, "filings": _filings(db, client.id)}
