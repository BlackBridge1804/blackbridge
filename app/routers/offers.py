"""
Partner/affiliate offers -- vendors you promote to clients (a secured card,
a credit-builder loan like Kovo, a rent-reporting service, and so on).
Managed only by the platform admin; every client (and staff) can see the
active ones. See models.PartnerOffer's docstring for the tradeline-specific
caveat -- this router doesn't vet what gets listed, that's on you.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_principal_optional
from ..database import get_db
from ..deps import require_platform_admin

router = APIRouter(prefix="/partner-offers", tags=["partner-offers"])


@router.get("", response_model=list[schemas.PartnerOfferOut])
def list_offers(
    all: bool = False,
    db: Session = Depends(get_db),
    principal=Depends(get_current_principal_optional),
):
    """Anyone -- including an anonymous visitor who hasn't signed up yet, e.g.
    on the "establish credit from scratch" walkthrough -- can see the ACTIVE
    offers. Only a logged-in platform admin can pass ?all=true to also see
    drafts/deactivated ones (for managing the list)."""
    query = db.query(models.PartnerOffer)
    if not (all and isinstance(principal, models.User) and principal.role == models.UserRole.platform_admin):
        query = query.filter(models.PartnerOffer.is_active == True)  # noqa: E712
    return query.order_by(models.PartnerOffer.created_at.desc()).all()


@router.post("", response_model=schemas.PartnerOfferOut, status_code=status.HTTP_201_CREATED)
def create_offer(
    payload: schemas.PartnerOfferCreate,
    db: Session = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    offer = models.PartnerOffer(**payload.model_dump())
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


@router.patch("/{offer_id}", response_model=schemas.PartnerOfferOut)
def update_offer(
    offer_id: str,
    payload: schemas.PartnerOfferUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    offer = db.query(models.PartnerOffer).filter(models.PartnerOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(offer, field, value)
    db.commit()
    db.refresh(offer)
    return offer


@router.delete("/{offer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_offer(offer_id: str, db: Session = Depends(get_db), _admin=Depends(require_platform_admin)):
    offer = db.query(models.PartnerOffer).filter(models.PartnerOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer not found")
    db.delete(offer)
    db.commit()
