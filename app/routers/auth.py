from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import create_access_token, hash_password, verify_password
from ..database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup/client", response_model=schemas.TokenOut, status_code=status.HTTP_201_CREATED)
def signup_client(payload: schemas.ClientSignup, db: Session = Depends(get_db)):
    """Every client signs up under a specific organization's branded instance
    (org_slug) -- this is how a licensee's customers end up scoped to that
    licensee's tenant rather than mixed in with everyone else's."""
    org = db.query(models.Organization).filter(models.Organization.slug == payload.org_slug).first()
    if not org or not org.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown organization")

    existing = (
        db.query(models.Client)
        .filter(models.Client.email == payload.email, models.Client.organization_id == org.id)
        .first()
    )
    if existing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An account with that email already exists here")

    client = models.Client(
        organization_id=org.id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(client)
    db.commit()
    db.refresh(client)

    token = create_access_token(subject=client.id, subject_type="client", organization_id=org.id)
    return schemas.TokenOut(access_token=token)


@router.post("/login", response_model=schemas.TokenOut)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Single login endpoint for both staff (User) and end consumers (Client),
    distinguished by which table the email is found in."""
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if user and verify_password(form_data.password, user.hashed_password):
        token = create_access_token(subject=user.id, subject_type="user", organization_id=user.organization_id)
        return schemas.TokenOut(access_token=token)

    client = db.query(models.Client).filter(models.Client.email == form_data.username).first()
    if client and verify_password(form_data.password, client.hashed_password):
        token = create_access_token(subject=client.id, subject_type="client", organization_id=client.organization_id)
        return schemas.TokenOut(access_token=token)

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
