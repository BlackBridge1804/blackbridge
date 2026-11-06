"""Receipt screens: a client sees their own; staff see their own organization's."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models
from ..auth import get_current_principal
from ..database import get_db
from ..models import UserRole
from ..receipts import Receipt, view

router = APIRouter(prefix="/receipts", tags=["receipts"])


def _can_see(principal, r: Receipt) -> bool:
    if isinstance(principal, models.Client):
        return r.client_id == principal.id
    if isinstance(principal, models.User):
        return principal.role == UserRole.platform_admin or principal.organization_id == r.organization_id
    return False


@router.get("/me")
def my_receipts(db: Session = Depends(get_db), principal=Depends(get_current_principal)):
    if not isinstance(principal, models.Client):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account only")
    rows = db.query(Receipt).filter(Receipt.client_id == principal.id).order_by(Receipt.paid_at.desc()).all()
    return {"receipts": [view(db, r) for r in rows]}


@router.get("/client/{client_id}")
def client_receipts(client_id: str, db: Session = Depends(get_db), principal=Depends(get_current_principal)):
    if not isinstance(principal, models.User):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff only")
    c = db.query(models.Client).filter(models.Client.id == client_id).first()
    if c is None or (principal.role != UserRole.platform_admin and c.organization_id != principal.organization_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    rows = db.query(Receipt).filter(Receipt.client_id == client_id).order_by(Receipt.paid_at.desc()).all()
    return {"receipts": [view(db, r) for r in rows]}


@router.get("/{receipt_id}")
def one_receipt(receipt_id: str, db: Session = Depends(get_db), principal=Depends(get_current_principal)):
    r = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if r is None or not _can_see(principal, r):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found")
    return view(db, r)
