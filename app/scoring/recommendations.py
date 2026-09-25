"""
Combines the rules-engine violations ("remove/dispute this") with the
tradeline-mix build suggestions ("add this") into one "what should I do
next" list -- the feature behind "scan my profile and tell me what to
remove, what to add, and roughly how much it matters."

Every item carries a factor tag, a qualitative impact tier, and a fixed
disclaimer. No item anywhere in this module ever carries a specific
point-number score prediction -- see app/scoring/factors.py for why.
"""
from .build_suggestions import generate_build_suggestions
from .factors import FACTOR_LABELS, RULE_ID_TO_FACTOR, ScoreFactor, impact_tier_for_factor

DISCLAIMER = (
    "This shows which scoring factor an item affects and roughly how much that factor "
    "typically weighs in a FICO Score -- it is not a prediction of your specific score or "
    "how many points it might change. Actual impact depends on your whole credit profile "
    "and the scoring model's proprietary calculation, which no third party has access to."
)


def build_recommendations(tradelines: list, violation_dicts: list[dict]) -> list[dict]:
    """violation_dicts: the output of app.rules.engine.scan_tradelines() (or the
    persisted Violation rows converted to the same dict shape)."""
    recommendations = []

    for v in violation_dicts:
        factor = RULE_ID_TO_FACTOR.get(v["rule_id"], ScoreFactor.payment_history)
        recommendations.append(
            {
                "suggestion_id": f"remove_{v['rule_id']}_{v.get('_tradeline_id', '')}",
                "category": "remove",
                "factor": factor.value,
                "factor_label": FACTOR_LABELS[factor],
                "impact_tier": impact_tier_for_factor(factor, v.get("severity", "medium")),
                "title": f"Dispute: {v['rule_id'].replace('_', ' ')}",
                "description": v["description"],
                "legal_basis": v["legal_basis"],
            }
        )

    for s in generate_build_suggestions(tradelines):
        factor = s["factor"]
        recommendations.append(
            {
                "suggestion_id": s["suggestion_id"],
                "category": "add",
                "factor": factor.value,
                "factor_label": FACTOR_LABELS[factor],
                "impact_tier": s["impact_tier"],
                "title": s["title"],
                "description": s["description"],
                "legal_basis": None,
            }
        )

    tier_order = {"high": 0, "medium": 1, "low": 2}
    recommendations.sort(key=lambda r: tier_order.get(r["impact_tier"], 3))
    return recommendations


def summarize_recommendations(recommendations: list[dict]) -> dict:
    """Free-tier view: counts by tier and category only, no descriptions."""
    by_tier: dict = {"high": 0, "medium": 0, "low": 0}
    by_category: dict = {"remove": 0, "add": 0}
    for r in recommendations:
        by_tier[r["impact_tier"]] = by_tier.get(r["impact_tier"], 0) + 1
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
    return {
        "total": len(recommendations),
        "by_impact_tier": by_tier,
        "by_category": by_category,
        "disclaimer": DISCLAIMER,
    }
