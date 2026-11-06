"""
Forgot-password.

Two ways to get a reset link, both one-time and expiring:
  * Self-service: the person enters their email on the login screen. If email is set up
    (see mailer.py) a link is emailed. If it is not, the request lands on the staff
    "Password help" list instead, and staff create a link and send it themselves.
  * Staff-created: staff create a link for a client (or the owner for a staff login) and
    pass it on by text or phone.

The link holds a random secret; only its SHA-256 is stored. Answers to the public
endpoints are always identical whether or not an account exists, so they cannot be used
to find out who has an account.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, String, func
from sqlalchemy.orm import Session

from .. import mailer, models
from ..auth import hash_password
from ..config import settings
from ..database import Base, engine, get_db
from ..deps import require_org_staff
from ..models import UserRole

SELF_MINUTES = 60
STAFF_HOURS = 24
MIN_PASSWORD = 8
GENERIC = "If we find an account for that email, we will help you set a new password."


def _uuid() -> str:
    return str(uuid.uuid4())


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id = Column(String, primary_key=True, default=_uuid)
    account_type = Column(String, nullable=False)  # "client" | "user"
    account_id = Column(String, nullable=False, index=True)
    organization_id = Column(String, nullable=True, index=True)  # no foreign key on purpose: see receipts.py
    kind = Column(String, nullable=False)  # "request" (waiting for staff) | "link"
    token_hash = Column(String, nullable=True, index=True)
    issued_by = Column(String, nullable=True)  # "self", "email" or a staff user id
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    used_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine, tables=[PasswordReset.__table__])

router = APIRouter(prefix="/password-reset", tags=["password reset"])


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _clients_by_email(db: Session, email: str, org_id: Optional[str] = None) -> list:
    q = db.query(models.Client).filter(
        models.Client.is_active == True,  # noqa: E712
        func.lower(models.Client.email) == email,
        ~models.Client.email.like("%@removed.invalid"),
    )
    if org_id:
        q = q.filter(models.Client.organization_id == org_id)
    return q.all()


def _user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.is_active == True, func.lower(models.User.email) == email).first()  # noqa: E712


def _new_link(db: Session, account_type: str, account_id: str, org_id: Optional[str], issued_by: str, minutes: int):
    """Close older open links/requests for the account, then make a fresh one. Returns (token, row)."""
    now = datetime.utcnow()
    for r in db.query(PasswordReset).filter(
        PasswordReset.account_type == account_type, PasswordReset.account_id == account_id,
        PasswordReset.used_at.is_(None), PasswordReset.closed_at.is_(None),
    ).all():
        r.closed_at = now
    token = secrets.token_urlsafe(32)
    row = PasswordReset(account_type=account_type, account_id=account_id, organization_id=org_id, kind="link",
                        token_hash=_hash(token), issued_by=issued_by, expires_at=now + timedelta(minutes=minutes))
    db.add(row)
    db.commit()
    return token, row


def _url(token: str, org_slug: Optional[str] = None) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/?reset={token}" + (f"&org={org_slug}" if org_slug else "")


def _business_name(db: Session, org_id: Optional[str]) -> str:
    from . import org_settings as _os
    org = db.query(models.Organization).filter(models.Organization.id == org_id).first() if org_id else None
    return _os.display_name_of(db, org) if org else "BlackBridge"


def _email_link(db: Session, to: str, org_id: Optional[str], url: str, minutes: int) -> None:
    name = _business_name(db, org_id)
    hours = f"{minutes // 60} hour(s)" if minutes >= 60 else f"{minutes} minutes"
    mailer.send_mail(
        to, f"Reset your {name} password",
        f"Hello,\n\nSomeone asked to reset the password for this email at {name}. "
        f"Open this link to choose a new password. It works once and expires in {hours}:\n\n{url}\n\n"
        "If you did not ask for this, you can ignore this email. Your password has not changed.\n\n" + name,
        from_name=name,
    )


# ---------------------------------------------------------------- public

class InfoOut(BaseModel):
    email_enabled: bool


@router.get("/info", response_model=InfoOut)
def info():
    return InfoOut(email_enabled=mailer.mail_enabled())


class RequestIn(BaseModel):
    email: str
    org: Optional[str] = None  # the organization slug from the client's own link, if any


@router.post("/request")
def request_reset(payload: RequestIn, db: Session = Depends(get_db)):
    email = _norm(payload.email)
    if "@" not in email or len(email) > 200:
        return {"ok": True, "message": GENERIC, "email_enabled": mailer.mail_enabled()}
    org_id = None
    if payload.org:
        org = db.query(models.Organization).filter(models.Organization.slug == payload.org).first()
        org_id = org.id if org else None
    now = datetime.utcnow()
    targets = [("client", c.id, c.organization_id, c.email) for c in _clients_by_email(db, email, org_id)]
    u = _user_by_email(db, email)
    if u is not None and (not org_id or u.organization_id == org_id or u.role == UserRole.platform_admin):
        targets.append(("user", u.id, u.organization_id, u.email))
    for account_type, account_id, oid, to in targets:
        # At most one request or link per account every 2 minutes, so this cannot be used to flood anyone.
        recent = db.query(PasswordReset).filter(
            PasswordReset.account_type == account_type, PasswordReset.account_id == account_id,
            PasswordReset.created_at > now - timedelta(minutes=2),
        ).first()
        if recent is not None:
            continue
        if mailer.mail_enabled():
            token, _row = _new_link(db, account_type, account_id, oid, "email", SELF_MINUTES)
            _email_link(db, to, oid, _url(token), SELF_MINUTES)
        elif account_type == "client":
            # No email set up: leave a request for staff to answer from the "Password help" list.
            open_req = db.query(PasswordReset).filter(
                PasswordReset.account_type == "client", PasswordReset.account_id == account_id,
                PasswordReset.kind == "request", PasswordReset.closed_at.is_(None), PasswordReset.used_at.is_(None),
            ).first()
            if open_req is None:
                db.add(PasswordReset(account_type="client", account_id=account_id, organization_id=oid,
                                     kind="request", issued_by="self"))
                db.commit()
    return {"ok": True, "message": GENERIC, "email_enabled": mailer.mail_enabled()}


class ConfirmIn(BaseModel):
    token: str
    new_password: str


@router.post("/confirm")
def confirm_reset(payload: ConfirmIn, db: Session = Depends(get_db)):
    if len(payload.new_password) < MIN_PASSWORD:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"The new password must be at least {MIN_PASSWORD} characters.")
    row = db.query(PasswordReset).filter(
        PasswordReset.token_hash == _hash(payload.token.strip()), PasswordReset.kind == "link").first()
    now = datetime.utcnow()
    if row is None or row.used_at is not None or row.closed_at is not None or (row.expires_at and row.expires_at < now):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This reset link has already been used or has expired. Please ask for a new one.")
    acct = (db.query(models.Client).filter(models.Client.id == row.account_id).first() if row.account_type == "client"
            else db.query(models.User).filter(models.User.id == row.account_id).first())
    if acct is None or not acct.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This reset link is no longer valid.")
    acct.hashed_password = hash_password(payload.new_password)
    row.used_at = now
    for r in db.query(PasswordReset).filter(
        PasswordReset.account_type == row.account_type, PasswordReset.account_id == row.account_id,
        PasswordReset.used_at.is_(None), PasswordReset.closed_at.is_(None),
    ).all():
        r.closed_at = now
    db.commit()
    return {"ok": True, "email": acct.email}


# ---------------------------------------------------------------- staff

def _scope_org(db: Session, principal: models.User, org_id: Optional[str]) -> str:
    """Org staff are confined to their own organization; the platform admin defaults to the main site."""
    if principal.role == UserRole.platform_admin:
        if org_id:
            return org_id
        main = db.query(models.Organization).filter(models.Organization.slug == settings.platform_org_slug).first()
        if main is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass ?org_id=<organization id>.")
        return main.id
    return principal.organization_id


@router.get("/staff/requests")
def staff_requests(org_id: Optional[str] = None, db: Session = Depends(get_db), principal=Depends(require_org_staff)):
    oid = _scope_org(db, principal, org_id)
    rows = db.query(PasswordReset).filter(
        PasswordReset.organization_id == oid, PasswordReset.kind == "request",
        PasswordReset.closed_at.is_(None), PasswordReset.used_at.is_(None),
    ).order_by(PasswordReset.created_at.desc()).limit(50).all()
    out = []
    for r in rows:
        c = db.query(models.Client).filter(models.Client.id == r.account_id).first()
        if c is None or not c.is_active:
            continue
        out.append({"id": r.id, "email": c.email, "name": c.full_name or "", "requested_at": r.created_at.isoformat()})
    return {"requests": out, "email_enabled": mailer.mail_enabled()}


class LinkIn(BaseModel):
    email: str
    org_id: Optional[str] = None


@router.post("/staff/link")
def staff_link(payload: LinkIn, db: Session = Depends(get_db), principal=Depends(require_org_staff)):
    email = _norm(payload.email)
    oid = _scope_org(db, principal, payload.org_id)
    clients = _clients_by_email(db, email, oid)
    target = None
    if clients:
        c = clients[0]
        target = ("client", c.id, c.organization_id, c.email, c.full_name or "")
    elif principal.role in (UserRole.platform_admin, UserRole.org_admin):
        u = _user_by_email(db, email)
        ok = u is not None and u.role != UserRole.platform_admin and (
            principal.role == UserRole.platform_admin
            or (u.organization_id == principal.organization_id and u.role == UserRole.org_staff))
        if ok:
            target = ("user", u.id, u.organization_id, u.email, "staff login")
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account with that email that you can reset.")
    account_type, account_id, target_org, to, name = target
    token, row = _new_link(db, account_type, account_id, target_org, principal.id, STAFF_HOURS * 60)
    org = db.query(models.Organization).filter(models.Organization.id == target_org).first() if target_org else None
    url = _url(token, org.slug if (org and account_type == "client") else None)
    emailed = False
    if mailer.mail_enabled():
        _email_link(db, to, target_org, url, STAFF_HOURS * 60)
        emailed = True
    return {"url": url, "email": to, "name": name, "expires_at": row.expires_at.isoformat(), "emailed": emailed}
