"""Platform-admin-only: onboard and manage licensee organizations."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import hash_password
from ..database import get_db
from ..deps import require_platform_admin
from ..models import UserRole

router = APIRouter(prefix="/organizations", tags=["organizations"])

_ASSIGNABLE_STAFF_ROLES = {UserRole.org_admin, UserRole.org_staff}


@router.post("", response_model=schemas.OrganizationOut, status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: schemas.OrganizationCreate,
    db: Session = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    existing = db.query(models.Organization).filter(models.Organization.slug == payload.slug).first()
    if existing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An organization with that slug already exists")

    org = models.Organization(name=payload.name, slug=payload.slug)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.get("", response_model=list[schemas.OrganizationOut])
def list_organizations(db: Session = Depends(get_db), _admin=Depends(require_platform_admin)):
    return db.query(models.Organization).all()


@router.post("/{org_slug}/staff", response_model=schemas.StaffOut, status_code=status.HTTP_201_CREATED)
def create_org_staff(
    org_slug: str,
    payload: schemas.StaffCreate,
    db: Session = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Gives a licensee their first (or another) login for the operator
    console (app/routers/staff.py) -- without this, a licensee organization
    exists but nobody there can actually log in and track their clients."""
    org = db.query(models.Organization).filter(models.Organization.slug == org_slug).first()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown organization")

    if payload.role not in _ASSIGNABLE_STAFF_ROLES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"role must be one of {sorted(r.value for r in _ASSIGNABLE_STAFF_ROLES)}",
        )

    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A staff account with that email already exists")

    user = models.User(
        organization_id=org.id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/{org_slug}/staff", response_model=list[schemas.StaffOut])
def list_org_staff(org_slug: str, db: Session = Depends(get_db), _admin=Depends(require_platform_admin)):
    org = db.query(models.Organization).filter(models.Organization.slug == org_slug).first()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown organization")
    return db.query(models.User).filter(models.User.organization_id == org.id).all()
