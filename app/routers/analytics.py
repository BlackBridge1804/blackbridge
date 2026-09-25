"""
Platform-admin-only analytics, built entirely from recorded outcomes (see
app/routers/letters.py's /outcome endpoint). This is what lets the business
actually learn which rules/letter types work, and is the only honest basis
for any marketed success-rate claim -- see the strategy doc's "Outcome
tracking is the single biggest unlock" section.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_platform_admin

router = APIRouter(prefix="/analytics", tags=["analytics"])

_SUCCESS_STATUSES = {"deleted", "updated"}


@router.get("/outcomes", response_model=schemas.OutcomeStatsOut)
def get_outcome_stats(db: Session = Depends(get_db), _admin=Depends(require_platform_admin)):
    letters = db.query(models.DisputeLetter).filter(models.DisputeLetter.outcome_status.isnot(None)).all()

    by_outcome_status: dict = {}
    by_rule_id: dict = {}

    for letter in letters:
        by_outcome_status[letter.outcome_status] = by_outcome_status.get(letter.outcome_status, 0) + 1

        rule_id = letter.violation.rule_id if letter.violation else "unknown"
        bucket = by_rule_id.setdefault(rule_id, {"total": 0, "success": 0})
        bucket["total"] += 1
        if letter.outcome_status in _SUCCESS_STATUSES:
            bucket["success"] += 1

    total = len(letters)
    success_count = sum(v for k, v in by_outcome_status.items() if k in _SUCCESS_STATUSES)
    success_rate = (success_count / total) if total else None

    return schemas.OutcomeStatsOut(
        total_letters_with_outcome=total,
        success_rate=success_rate,
        by_outcome_status=by_outcome_status,
        by_rule_id=by_rule_id,
    )
