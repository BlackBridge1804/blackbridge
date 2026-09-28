"""
The licensed operator's console: lets an organization's own staff (org_admin
/ org_staff) -- or the platform admin, looking at any organization -- track
where every one of their clients' reports stands, without exposing letter
text or full violation detail (that stays the client's own, behind the
client login). This is read-only progress tracking, not a support inbox.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_org_staff
from ..models import UserRole
from .reports import load_progress_for_report

router = APIRouter(prefix="/staff", tags=["staff"])


def _resolve_org_id(principal: models.User, org_id: str = None) -> str:
    if principal.role == UserRole.platform_admin:
        if not org_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pass ?org_id=<organization id> -- platform admins can view any organization's "
                "console, so one must be specified.",
            )
        return org_id
    # org_admin / org_staff are always confined to their own organization,
    # regardless of what (if anything) they pass in org_id.
    return principal.organization_id


@router.get("/clients", response_model=list[schemas.StaffClientOut])
def list_org_clients(
    org_id: str = Query(default=None),
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    resolved_org_id = _resolve_org_id(principal, org_id)
    clients = (
        db.query(models.Client)
        .filter(models.Client.organization_id == resolved_org_id)
        .order_by(models.Client.created_at.desc())
        .all()
    )

    out = []
    for c in clients:
        reports = (
            db.query(models.Report)
            .filter(models.Report.client_id == c.id)
            .order_by(models.Report.created_at.desc())
            .all()
        )
        latest_progress = load_progress_for_report(db, reports[0]) if reports else None
        out.append(
            schemas.StaffClientOut(
                id=c.id,
                email=c.email,
                full_name=c.full_name,
                created_at=c.created_at,
                report_count=len(reports),
                latest_report_progress=latest_progress,
            )
        )
    return out


@router.get("/clients/{client_id}/reports", response_model=list[schemas.ReportProgressOut])
def list_client_reports(
    client_id: str,
    db: Session = Depends(get_db),
    principal: models.User = Depends(require_org_staff),
):
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if principal.role != UserRole.platform_admin and client.organization_id != principal.organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-tenant access denied")

    reports = (
        db.query(models.Report)
        .filter(models.Report.client_id == client.id)
        .order_by(models.Report.created_at.desc())
        .all()
    )
    return [load_progress_for_report(db, r) for r in reports]
