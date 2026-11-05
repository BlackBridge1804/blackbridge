"""
Per-organization (licensee) settings: logo, brand color, business name, address,
payment instructions and prices.

Rules:
  * A licensee's clients see the LICENSEE's name, logo, color, payment info and
    prices -- never BlackBridge's Zelle account.
  * The main site's organization (PLATFORM_ORG_SLUG) keeps using the Render
    environment settings for payment info, prices and address (so nothing
    changes for BlackBridge itself). It can still set a logo, color and name here.
  * Only an organization admin can change settings; other staff can read them.
  * Logos are small PNG/JPEG/WEBP images stored in the database (the free host
    wipes uploaded files on restart, so files on disk would vanish). SVG is not
    allowed because it can carry scripts.
"""
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import Base, engine, get_db
from ..deps import require_org_staff
from ..models import UserRole

MAX_LOGO_CHARS = 400_000  # about 300 KB of image
LOGO_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PRICE_RE = re.compile(r"^\d{1,5}(\.\d{2})?$")


class OrgSettings(Base):
    __tablename__ = "org_settings"

    organization_id = Column(String, ForeignKey("organizations.id"), primary_key=True)
    display_name = Column(String, nullable=True)
    logo_data = Column(Text, nullable=True)
    primary_color = Column(String, nullable=True)
    business_address = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    zelle_contact = Column(String, nullable=True)
    zelle_name = Column(String, nullable=True)
    zelle_note = Column(String, nullable=True)
    price_report = Column(String, nullable=True)
    price_bundle = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrgSettingsExtra(Base):
    """More licensee settings. A separate table (not new columns) because the live
    database only creates missing tables; it never adds columns to existing ones."""
    __tablename__ = "org_settings_extra"

    organization_id = Column(String, ForeignKey("organizations.id"), primary_key=True)
    website = Column(String, nullable=True)
    support_hours = Column(String, nullable=True)
    payment_instructions = Column(Text, nullable=True)
    refund_mode = Column(String, nullable=True)  # "final" (default) or "custom"
    refund_text = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine, tables=[OrgSettings.__table__, OrgSettingsExtra.__table__])

router = APIRouter(prefix="/org-settings", tags=["org settings"])


def is_platform_org(org: Optional[models.Organization]) -> bool:
    return bool(org) and org.slug == settings.platform_org_slug


def get_row(db: Session, organization_id: str) -> Optional[OrgSettings]:
    return db.query(OrgSettings).filter(OrgSettings.organization_id == organization_id).first()


def get_extra(db: Session, organization_id: str) -> Optional[OrgSettingsExtra]:
    return db.query(OrgSettingsExtra).filter(OrgSettingsExtra.organization_id == organization_id).first()


def display_name_of(db: Session, org: models.Organization) -> str:
    row = get_row(db, org.id)
    if row and row.display_name:
        return row.display_name
    return settings.platform_display_name if is_platform_org(org) else org.name


def refund_policy_for(db: Session, org: models.Organization) -> dict:
    """The refund policy this business's clients agree to. {"mode", "text"}"""
    name = display_name_of(db, org)
    x = get_extra(db, org.id)
    if is_platform_org(org):
        name = "BlackBridge"
    if x and x.refund_mode == "custom" and (x.refund_text or "").strip() and not is_platform_org(org):
        return {"mode": "custom", "text": x.refund_text.strip()}
    return {
        "mode": "final",
        "text": (
            "Your full breakdown and letters are delivered digitally the moment you unlock them. "
            "You can cancel at any time and we will not charge you again, but once you have access "
            f"there is no refund. The only exception is if {name} was unable to read your credit report. "
            "Results are not guaranteed."
        ),
    }


def brand_for(db: Session, org: Optional[models.Organization]) -> dict:
    """What a visitor sees: name, logo, color. Nothing private."""
    if org is None:
        return {"name": "BlackBridge", "logo": None, "color": None}
    row = get_row(db, org.id)
    return {
        "name": display_name_of(db, org),
        "logo": (row.logo_data if row and row.logo_data else None),
        "color": (row.primary_color if row and row.primary_color else None),
    }


def effective(db: Session, org: models.Organization) -> dict:
    """The settings clients of this organization are shown."""
    row = get_row(db, org.id)

    def val(name):
        return ((getattr(row, name) if row else None) or "").strip()

    x = get_extra(db, org.id)
    instructions = ((x.payment_instructions if x else None) or "").strip()
    if is_platform_org(org):
        return {
            "payment_instructions": "",
            "address": settings.business_address.strip(),
            "zelle_contact": settings.zelle_contact.strip(),
            "zelle_name": settings.zelle_name.strip(),
            "zelle_note": settings.zelle_note.strip(),
            "price_report": settings.price_report,
            "price_bundle": settings.price_bundle,
            "platform": True,
        }
    return {
        "payment_instructions": instructions,
        "address": val("business_address"),
        "zelle_contact": val("zelle_contact"),
        "zelle_name": val("zelle_name"),
        "zelle_note": val("zelle_note"),
        "price_report": val("price_report") or settings.price_report,
        "price_bundle": val("price_bundle") or settings.price_bundle,
        "platform": False,
    }


@router.get("/public/{slug}")
def public_brand(slug: str, db: Session = Depends(get_db)):
    org = db.query(models.Organization).filter(models.Organization.slug == slug).first()
    if not org or not org.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown organization")
    out = brand_for(db, org)
    row, x = get_row(db, org.id), get_extra(db, org.id)
    if is_platform_org(org):
        out["contact"] = {"phone": row.phone if row and row.phone else "(669) 272-2854",
                          "email": row.email if row and row.email else "BlackBridgeoffice@Gmail.com",
                          "address": settings.business_address.strip(),
                          "website": (x.website if x else None) or "", "hours": (x.support_hours if x else None) or ""}
    else:
        out["contact"] = {"phone": (row.phone if row else None) or "", "email": (row.email if row else None) or "",
                          "address": (row.business_address if row else None) or "",
                          "website": (x.website if x else None) or "", "hours": (x.support_hours if x else None) or ""}
    out["refund_policy"] = refund_policy_for(db, org)["text"]
    out["slug"] = org.slug
    out["is_platform"] = is_platform_org(org)
    return out


def _my_org(db: Session, principal: models.User) -> models.Organization:
    if principal.organization_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The platform owner account has no business settings of its own. Log in as an organization admin.",
        )
    org = db.query(models.Organization).filter(models.Organization.id == principal.organization_id).first()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return org


def _out(db: Session, org: models.Organization, principal: models.User) -> dict:
    row = get_row(db, org.id)

    def g(n):
        return (getattr(row, n) if row else None) or ""

    return {
        "organization_name": org.name,
        "slug": org.slug,
        "client_link": f"/?org={org.slug}",
        "can_edit": principal.role in (UserRole.org_admin, UserRole.platform_admin),
        "uses_environment_for_payments": is_platform_org(org),
        "display_name": g("display_name"),
        "logo": g("logo_data"),
        "primary_color": g("primary_color"),
        "business_address": g("business_address"),
        "phone": g("phone"),
        "email": g("email"),
        "zelle_contact": g("zelle_contact"),
        "zelle_name": g("zelle_name"),
        "zelle_note": g("zelle_note"),
        "price_report": g("price_report"),
        "price_bundle": g("price_bundle"),
        "website": (get_extra(db, org.id).website if get_extra(db, org.id) else "") or "",
        "support_hours": (get_extra(db, org.id).support_hours if get_extra(db, org.id) else "") or "",
        "payment_instructions": (get_extra(db, org.id).payment_instructions if get_extra(db, org.id) else "") or "",
        "refund_mode": (get_extra(db, org.id).refund_mode if get_extra(db, org.id) else "") or "final",
        "refund_text": (get_extra(db, org.id).refund_text if get_extra(db, org.id) else "") or "",
        "public_page": f"/static/business.html?org={org.slug}",
    }


@router.get("/me")
def get_my_settings(db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    return _out(db, _my_org(db, principal), principal)


class SettingsIn(BaseModel):
    display_name: Optional[str] = None
    logo: Optional[str] = None  # data URL, "" removes it
    primary_color: Optional[str] = None
    business_address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    zelle_contact: Optional[str] = None
    zelle_name: Optional[str] = None
    zelle_note: Optional[str] = None
    price_report: Optional[str] = None
    price_bundle: Optional[str] = None
    website: Optional[str] = None
    support_hours: Optional[str] = None
    payment_instructions: Optional[str] = None
    refund_mode: Optional[str] = None
    refund_text: Optional[str] = None


_LIMITS = {
    "display_name": 80, "business_address": 200, "phone": 40, "email": 120,
    "zelle_contact": 120, "zelle_name": 80, "zelle_note": 400,
}


@router.put("/me")
def save_my_settings(
    payload: SettingsIn, db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)
):
    if principal.role not in (UserRole.org_admin, UserRole.platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can change these settings.")
    org = _my_org(db, principal)
    row = get_row(db, org.id)
    if row is None:
        row = OrgSettings(organization_id=org.id)
        db.add(row)
    data = payload.model_dump(exclude_unset=True)

    for key, limit in _LIMITS.items():
        if key in data:
            v = (data[key] or "").strip()
            if len(v) > limit:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key.replace('_', ' ').capitalize()} is too long (max {limit} characters).")
            if "<" in v or ">" in v:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key.replace('_', ' ').capitalize()} cannot contain < or >.")
            setattr(row, key, v or None)
    if "primary_color" in data:
        v = (data["primary_color"] or "").strip()
        if v and not COLOR_RE.match(v):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Color must look like #2F6FED.")
        row.primary_color = v or None
    for key in ("price_report", "price_bundle"):
        if key in data:
            v = (data[key] or "").strip()
            if v and not PRICE_RE.match(v):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Prices must be numbers like 60 or 60.00.")
            setattr(row, key, v or None)
    if "logo" in data:
        v = (data["logo"] or "").strip()
        if v:
            if len(v) > MAX_LOGO_CHARS:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "That logo is too large. Use an image under about 300 KB.")
            if not LOGO_RE.match(v):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "The logo must be a PNG, JPEG or WEBP image.")
        row.logo_data = v or None
    x = get_extra(db, org.id)
    if x is None:
        x = OrgSettingsExtra(organization_id=org.id)
        db.add(x)
    for key, limit in (("website", 160), ("support_hours", 120), ("payment_instructions", 800), ("refund_text", 2000)):
        if key in data:
            v = (data[key] or "").strip()
            if len(v) > limit:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key.replace('_', ' ').capitalize()} is too long (max {limit} characters).")
            if "<" in v or ">" in v:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key.replace('_', ' ').capitalize()} cannot contain < or >.")
            if key == "website" and v and not re.match(r"^https?://[^\s]+$", v):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Website must start with http:// or https://")
            setattr(x, key, v or None)
    if "refund_mode" in data:
        v = (data["refund_mode"] or "final").strip()
        if v not in ("final", "custom"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Refund policy must be 'final' or 'custom'.")
        if v == "custom" and not (data.get("refund_text") or (x.refund_text or "")).strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please write your refund policy text.")
        x.refund_mode = v
    db.commit()
    return _out(db, org, principal)


# ----------------------------------------------------------------- team
class TeamIn(BaseModel):
    email: str
    password: str
    role: str = "org_staff"


class ActiveIn(BaseModel):
    active: bool


def _team_admin(db: Session, principal: models.User) -> models.Organization:
    if principal.role != UserRole.org_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can manage the team.")
    return _my_org(db, principal)


def _member(u: models.User) -> dict:
    return {"id": u.id, "email": u.email, "role": u.role.value if hasattr(u.role, "value") else u.role, "is_active": bool(u.is_active)}


@router.get("/team")
def team_list(db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    org = _team_admin(db, principal)
    return [_member(u) for u in db.query(models.User).filter(models.User.organization_id == org.id).order_by(models.User.email).all()]


@router.post("/team", status_code=status.HTTP_201_CREATED)
def team_add(payload: TeamIn, db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    org = _team_admin(db, principal)
    email = payload.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please enter a valid email address.")
    if len(payload.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The password must be at least 8 characters.")
    if payload.role not in ("org_staff", "org_admin"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Role must be org_staff or org_admin.")
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A staff login with that email already exists.")
    from ..auth import hash_password
    u = models.User(organization_id=org.id, email=email, hashed_password=hash_password(payload.password), role=UserRole(payload.role))
    db.add(u)
    db.commit()
    return _member(u)


@router.post("/team/{user_id}/active")
def team_active(user_id: str, payload: ActiveIn, db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    org = _team_admin(db, principal)
    u = db.query(models.User).filter(models.User.id == user_id, models.User.organization_id == org.id).first()
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team member not found.")
    if u.id == principal.id and not payload.active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot turn off your own login.")
    u.is_active = payload.active
    db.commit()
    return _member(u)


# ------------------------------------------------------------ client export
def _csv_safe(v) -> str:
    t = "" if v is None else str(v)
    return "'" + t if t[:1] in ("=", "+", "-", "@", "\t", "\r") else t


@router.get("/export/clients.csv")
def export_clients(db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    """Your client list as a spreadsheet file (for your records, or if your license ends)."""
    import csv
    import io

    from fastapi.responses import Response

    org = _my_org(db, principal)
    if principal.role != UserRole.org_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can export the client list.")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "signed_up", "reports_uploaded", "reports_unlocked"])
    clients = (
        db.query(models.Client)
        .filter(models.Client.organization_id == org.id, ~models.Client.email.like("%@removed.invalid"))
        .order_by(models.Client.created_at)
        .all()
    )
    for c in clients:
        reps = db.query(models.Report).filter(models.Report.client_id == c.id).all()
        w.writerow([_csv_safe(c.email), _csv_safe(c.full_name), c.created_at.date().isoformat() if c.created_at else "",
                    len(reps), sum(1 for r in reps if r.is_paid)])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{org.slug}-clients.csv"'})
