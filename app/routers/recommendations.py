"""
'What should I do next' -- combines dispute findings (remove) with
credit-building suggestions (add) into one prioritized list. Same paywall
shape as reports/letters: free tier gets counts, paid tier gets the
full, described list.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import assert_same_org, require_client
from ..scoring.recommendations import build_recommendations, summarize_recommendations

router = APIRouter(prefix="/reports/{report_id}/recommendations", tags=["recommendations"])


def _get_owned_report(db: Session, report_id: str, client: models.Client) -> models.Report:
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    assert_same_org(client, report.organization_id)
    if report.client_id != client.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your report")
    return report


def _load_recommendations(db: Session, report: models.Report) -> list[dict]:
    tradelines = db.query(models.Tradeline).filter(models.Tradeline.report_id == report.id).all()
    violations = (
        db.query(models.Violation)
        .join(models.Tradeline)
        .filter(models.Tradeline.report_id == report.id)
        .all()
    )
    violation_dicts = [
        {
            "rule_id": v.rule_id,
            "legal_basis": v.legal_basis,
            "severity": v.severity,
            "description": v.description,
            "letter_type": v.letter_type,
            "_tradeline_id": v.tradeline_id,
        }
        for v in violations
    ]
    return build_recommendations(tradelines, violation_dicts)


@router.get("/summary", response_model=schemas.RecommendationsSummaryOut)
def get_recommendations_summary(
    report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)
):
    report = _get_owned_report(db, report_id, client)
    recommendations = _load_recommendations(db, report)
    return summarize_recommendations(recommendations)


@router.get("", response_model=list[schemas.RecommendationOut])
def get_recommendations(
    report_id: str, db: Session = Depends(get_db), client: models.Client = Depends(require_client)
):
    report = _get_owned_report(db, report_id, client)
    if not report.is_paid:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Full recommendations unlock after checkout for this report -- see /billing/checkout/{report_id}",
        )
    return _load_recommendations(db, report)
