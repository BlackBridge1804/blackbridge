from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import Client, User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str, subject_type: str, organization_id: Optional[str]) -> str:
    """subject_type is 'user' or 'client' -- lets one token scheme serve both staff and end clients."""
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": subject,
        "type": subject_type,
        "org": organization_id,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def get_current_principal(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """
    Returns either a User (staff) or a Client (end consumer), decided by token type.
    Every downstream route checks `.organization_id` on whatever comes back to
    enforce tenant isolation -- see app/deps.py.
    """
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(token)
    subject_type = payload.get("type")
    subject_id = payload.get("sub")

    if subject_type == "user":
        principal = db.query(User).filter(User.id == subject_id, User.is_active == True).first()  # noqa: E712
    elif subject_type == "client":
        principal = db.query(Client).filter(Client.id == subject_id, Client.is_active == True).first()  # noqa: E712
    else:
        principal = None

    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found or inactive")
    return principal


def get_current_principal_optional(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Same as get_current_principal but returns None instead of raising when
    there's no token -- for endpoints (like the public partner-offers list)
    that should work for anonymous visitors and just show less/less-privileged
    content when nobody's logged in. An invalid/expired token still raises,
    same as above, so a stale token doesn't silently degrade to "anonymous."
    """
    if not token:
        return None
    return get_current_principal(token=token, db=db)
