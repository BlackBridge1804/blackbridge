"""
The in-app help/walkthrough chatbot. See app/assistant.py for the actual
prompt and legal guardrails -- this router's only job is assembling the
grounding context from data the platform has ALREADY computed (never letting
the model see raw, un-vetted report text) and keeping the conversation
scoped to one client's own account.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..assistant import get_assistant_reply
from ..config import settings
from ..database import get_db
from ..deps import require_client
from .recommendations import _load_recommendations
from .reports import _get_owned_report, load_progress_for_report

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _build_context(db: Session, client: models.Client, report_id: str) -> dict:
    if not report_id:
        return None
    try:
        report = _get_owned_report(db, report_id, client)
    except HTTPException:
        # Don't let a bad/foreign report_id break the chat -- just answer
        # without report-specific grounding.
        return None

    progress = load_progress_for_report(db, report)
    context = {"report_progress": progress}

    if report.is_paid:
        violations = (
            db.query(models.Violation)
            .join(models.Tradeline)
            .filter(models.Tradeline.report_id == report.id)
            .all()
        )
        context["violations"] = [
            {
                "rule_id": v.rule_id,
                "legal_basis": v.legal_basis,
                "severity": v.severity,
                "description": v.description,
            }
            for v in violations
        ]

        letters = (
            db.query(models.DisputeLetter)
            .join(models.Violation)
            .join(models.Tradeline)
            .filter(models.Tradeline.report_id == report.id)
            .all()
        )
        context["letters"] = [
            {
                "letter_type": l.letter_type,
                "recipient": l.recipient,
                "outcome_status": l.outcome_status,
            }
            for l in letters
        ]

        recommendations = _load_recommendations(db, report)
        context["recommendations"] = [
            {
                "category": r["category"],
                "factor_label": r["factor_label"],
                "impact_tier": r["impact_tier"],
                "title": r["title"],
                "description": r["description"],
            }
            for r in recommendations
        ]
    else:
        context["note"] = "This report's full breakdown is not yet unlocked -- only summary counts are available."

    return context


@router.post("/chat", response_model=schemas.AssistantChatOut)
def chat(
    payload: schemas.AssistantChatIn,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    if not payload.message.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "message can't be empty")

    context = _build_context(db, client, payload.report_id)
    # Cap history so a long-running chat can't balloon the request forever.
    history = payload.history[-20:] if payload.history else []

    reply = get_assistant_reply(
        message=payload.message,
        history=history,
        context=context,
        anthropic_api_key=settings.anthropic_api_key,
    )
    return schemas.AssistantChatOut(reply=reply, grounded=context is not None)
