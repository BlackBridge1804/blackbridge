"""
Client mailing address and service type.

Why a separate table instead of new columns on `clients`: the live database
is created with Base.metadata.create_all, which creates missing TABLES but
never adds columns to a table that already exists. A new table appears
automatically on the next deploy, with no manual database step.

service_type:
  "diy"          -- the client files their own disputes (the default)
  "done_for_you" -- staff files on the client's behalf. That is a different
                    service with its own legal requirements (written contract,
                    consumer rights notice, cancellation right, payment
                    timing, state rules, signed authorization). Get an
                    attorney's sign-off on those BEFORE doing it for a client.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.orm import Session

from .. import models
from ..database import Base, engine, get_db
from ..deps import require_client, require_org_staff
from ..models import UserRole

VALID_SERVICE_TYPES = {"diy", "done_for_you"}


class ClientProfile(Base):
    __tablename__ = "client_profiles"

    client_id = Column(String, ForeignKey("clients.id"), primary_key=True)
    address_line1 = Column(String, nullable=True)
    address_line2 = Column(String, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    zip_code = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    service_type = Column(String, nullable=False, default="diy")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Create just this table now (idempotent), so it exists no matter what order
# the app's modules are imported in.
Base.metadata.create_all(bind=engine, tables=[ClientProfile.__table__])


class ProfileIn(BaseModel):
    address_line1: str = Field(min_length=1, max_length=120)
    address_line2: Optional[str] = Field(default=None, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=2, max_length=40)
    zip_code: str = Field(min_length=3, max_length=15)
    phone: Optional[str] = Field(default=None, max_length=30)


class ProfileOut(BaseModel):
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    phone: Optional[str] = None
    service_type: str = "diy"
    has_address: bool = False


class ServiceTypeIn(BaseModel):
    service_type: str


def _has_address(p: Optional[ClientProfile]) -> bool:
    return bool(p and p.address_line1 and p.city and p.state and p.zip_code)


def _to_out(p: Optional[ClientProfile]) -> ProfileOut:
    if not p:
        return ProfileOut()
    return ProfileOut(
        address_line1=p.address_line1,
        address_line2=p.address_line2,
        city=p.city,
        state=p.state,
        zip_code=p.zip_code,
        phone=p.phone,
        service_type=p.service_type or "diy",
        has_address=_has_address(p),
    )


def format_mailing_address(p: ClientProfile) -> str:
    lines = [p.address_line1]
    if p.address_line2:
        lines.append(p.address_line2)
    lines.append(f"{p.city}, {p.state} {p.zip_code}")
    return "\n".join(lines)


def require_mailing_address(db: Session, client: models.Client) -> str:
    """Used by the letter generators: a letter must never go out with a
    placeholder return address."""
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client.id).first()
    if not _has_address(p):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Please add your mailing address first. Go back to your reports page and fill in "
            "the Mailing address card, then generate your letters again.",
        )
    return format_mailing_address(p)


# ---- client-facing -------------------------------------------------------
router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("/me", response_model=ProfileOut)
def get_my_profile(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client.id).first()
    return _to_out(p)


@router.put("/me", response_model=ProfileOut)
def save_my_profile(
    payload: ProfileIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client.id).first()
    if not p:
        p = ClientProfile(client_id=client.id, service_type="diy")
        db.add(p)
    p.address_line1 = payload.address_line1.strip()
    p.address_line2 = (payload.address_line2 or "").strip() or None
    p.city = payload.city.strip()
    p.state = payload.state.strip()
    p.zip_code = payload.zip_code.strip()
    p.phone = (payload.phone or "").strip() or None
    db.commit()
    db.refresh(p)
    return _to_out(p)


# ---- staff-facing --------------------------------------------------------
staff_router = APIRouter(tags=["staff-profile"])


def _client_for_staff(db: Session, client_id: str, principal: models.User) -> models.Client:
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if principal.role != UserRole.platform_admin and client.organization_id != principal.organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-tenant access denied")
    return client


@staff_router.get("/clients/{client_id}/profile", response_model=ProfileOut)
def get_client_profile(
    client_id: str,
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    client = _client_for_staff(db, client_id, principal)
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client.id).first()
    return _to_out(p)


@staff_router.put("/clients/{client_id}/service-type", response_model=ProfileOut)
def set_client_service_type(
    client_id: str,
    payload: ServiceTypeIn,
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    if payload.service_type not in VALID_SERVICE_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"service_type must be one of {sorted(VALID_SERVICE_TYPES)}",
        )
    client = _client_for_staff(db, client_id, principal)
    p = db.query(ClientProfile).filter(ClientProfile.client_id == client.id).first()
    if not p:
        p = ClientProfile(client_id=client.id)
        db.add(p)
    p.service_type = payload.service_type
    db.commit()
    db.refresh(p)
    return _to_out(p)
