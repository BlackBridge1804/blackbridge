"""
Remove a client.

What "remove" does:
  * Deletes the client's personal data: reports, parsed accounts, findings,
    letters, mailing address, and saved identity-theft case details.
  * Frees the email address, so the same person can sign up again later.
  * Signs the client out for good (the login can never work again).

What it keeps, on purpose:
  * The signed agreement, the signed CFPB authorization, and the log of
    complaints filed for the client. The consumer rights law asks a credit
    repair business to keep a copy of the signed disclosure for two years, so
    these are not deleted here.
  * A short summary of the identity-theft confirmations the client made
    (date, time, IP address, report number, wording version) with no account names.
  * A removal record: who removed the client, and when.

Only an organization admin (or the platform admin) can do this. The staff member
must type the client's email to confirm. Deleted data cannot be recovered.

Draft for attorney review. Not legal advice.
"""
import json
import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from .. import models
from ..auth import hash_password
from ..database import Base, engine, get_db
from ..deps import assert_same_org, require_org_staff
from ..models import UserRole


def _uuid() -> str:
    return str(uuid.uuid4())


class ClientRemoval(Base):
    __tablename__ = "client_removals"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    former_client_id = Column(String, nullable=False, index=True)
    removed_by_user_id = Column(String, ForeignKey("users.id"), nullable=True)
    removed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reports_removed = Column(Integer, default=0)
    retained_note = Column(Text, nullable=True)  # JSON: identity-theft confirmation summary


Base.metadata.create_all(bind=engine, tables=[ClientRemoval.__table__])

REMOVED_DOMAIN = "removed.invalid"

router = APIRouter(tags=["client removal"])


class RemoveIn(BaseModel):
    confirm_email: str


@router.post("/staff/client/{client_id}/remove")
def remove_client(
    client_id: str,
    payload: RemoveIn,
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    if principal.role not in (UserRole.org_admin, UserRole.platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can remove a client.")
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    assert_same_org(principal, client.organization_id)
    if client.email.endswith("@" + REMOVED_DOMAIN):
        raise HTTPException(status.HTTP_409_CONFLICT, "This client was already removed.")
    if (payload.confirm_email or "").strip().lower() != client.email.strip().lower():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The email you typed doesn't match this client. Nothing was removed.")

    # Imported here so this module works even if a related module is missing.
    from .authorizations import ClientAuthorization
    from .idtheft import IdTheftAttestation, IdTheftCase
    from .profile import ClientProfile

    report_ids = [r.id for r in db.query(models.Report.id).filter(models.Report.client_id == client.id).all()]
    tradeline_ids = []
    violation_ids = []
    if report_ids:
        tradeline_ids = [t.id for t in db.query(models.Tradeline.id).filter(models.Tradeline.report_id.in_(report_ids)).all()]
    if tradeline_ids:
        violation_ids = [v.id for v in db.query(models.Violation.id).filter(models.Violation.tradeline_id.in_(tradeline_ids)).all()]

    # Keep a short, account-free summary of the identity-theft confirmations.
    attestations = db.query(IdTheftAttestation).filter(IdTheftAttestation.client_id == client.id).all()
    retained = [
        {
            "confirmed_at": a.confirmed_at.isoformat() if a.confirmed_at else None,
            "confirmed_ip": a.confirmed_ip,
            "report_number": a.report_number,
            "text_version": a.text_version,
        }
        for a in attestations
    ]

    # Delete from the bottom up so no row points at one that is already gone.
    if violation_ids:
        db.query(models.DisputeLetter).filter(models.DisputeLetter.violation_id.in_(violation_ids)).delete(synchronize_session=False)
        db.query(models.Violation).filter(models.Violation.id.in_(violation_ids)).delete(synchronize_session=False)
    db.query(IdTheftAttestation).filter(IdTheftAttestation.client_id == client.id).delete(synchronize_session=False)
    if tradeline_ids:
        db.query(models.Tradeline).filter(models.Tradeline.id.in_(tradeline_ids)).delete(synchronize_session=False)
    db.query(IdTheftCase).filter(IdTheftCase.client_id == client.id).delete(synchronize_session=False)
    if report_ids:
        db.query(models.Report).filter(models.Report.id.in_(report_ids)).delete(synchronize_session=False)
    db.query(ClientProfile).filter(ClientProfile.client_id == client.id).delete(synchronize_session=False)

    # Any active CFPB authorization ends now.
    now = datetime.utcnow()
    for a in db.query(ClientAuthorization).filter(ClientAuthorization.client_id == client.id, ClientAuthorization.revoked_at.is_(None)).all():
        a.revoked_at = now

    # Turn the account into an empty shell nobody can log in to, and free the email.
    client.email = f"removed-{client.id[:8]}@{REMOVED_DOMAIN}"
    client.full_name = "Removed client"
    client.hashed_password = hash_password(secrets.token_urlsafe(32))
    client.is_active = False

    db.add(ClientRemoval(
        organization_id=client.organization_id,
        former_client_id=client.id,
        removed_by_user_id=principal.id,
        removed_at=now,
        reports_removed=len(report_ids),
        retained_note=json.dumps(retained) if retained else None,
    ))
    db.commit()
    return {
        "ok": True,
        "reports_removed": len(report_ids),
        "kept": "The signed agreement and authorization records are kept for the legally required period.",
    }
