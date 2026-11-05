"""Change your own password (staff or client)."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_current_principal, hash_password, verify_password
from ..database import get_db

router = APIRouter(prefix="/account", tags=["account"])


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(payload: PasswordIn, db: Session = Depends(get_db), principal=Depends(get_current_principal)):
    if not verify_password(payload.current_password, principal.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is not right.")
    if len(payload.new_password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The new password must be at least 8 characters.")
    if payload.new_password == payload.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please choose a different password.")
    principal.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}
