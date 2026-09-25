"""
"Add" suggestions -- forward-looking credit-building moves, derived from a
tradeline-mix analysis of the parsed report. Heuristic and rule-based, same
spirit as the dispute rules engine: every suggestion traces back to a
specific, checkable fact about the file, not a model guess.

Deliberately does NOT include "become an authorized user on a stranger's
account" / tradeline-for-sale suggestions. That industry is under increasing
FTC/CFPB scrutiny, modern FICO models are built to discount purchased
authorized-user lines, and it's a liability for a licensed platform, not a
real growth feature. Authorized-user status is only suggested in the
"someone you actually know and trust" sense.
"""
from datetime import date
from typing import Optional

from .factors import ScoreFactor, impact_tier_for_factor

UTILIZATION_TARGET = 0.30  # the commonly cited "keep it under this" revolving utilization line
THIN_FILE_ACCOUNT_THRESHOLD = 2
SHORT_HISTORY_YEARS = 2.0


def _parse_date(value: Optional[str]):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _revolving_utilization(tradelines: list) -> Optional[float]:
    total_balance = 0
    total_limit = 0
    for t in tradelines:
        if (t.account_type or "").lower() == "revolving" and t.credit_limit:
            total_balance += t.balance or 0
            total_limit += t.credit_limit
    if total_limit <= 0:
        return None
    return total_balance / total_limit


def _oldest_account_age_years(tradelines: list, today: Optional[date] = None) -> Optional[float]:
    today = today or date.today()
    open_dates = [d for d in (_parse_date(t.date_opened) for t in tradelines) if d]
    if not open_dates:
        return None
    oldest = min(open_dates)
    return (today - oldest).days / 365.25


def generate_build_suggestions(tradelines: list) -> list[dict]:
    suggestions = []
    active_tradelines = [t for t in tradelines if not t.is_collection]
    account_types = {(t.account_type or "").lower() for t in active_tradelines}

    if len(active_tradelines) < THIN_FILE_ACCOUNT_THRESHOLD:
        suggestions.append(
            {
                "suggestion_id": "build_thin_file",
                "category": "add",
                "factor": ScoreFactor.length_of_history,
                "impact_tier": impact_tier_for_factor(ScoreFactor.length_of_history, "high"),
                "title": "Build a credit file with a low-risk starter product",
                "description": (
                    "This file has very few open tradelines. A secured credit card or a small "
                    "credit-builder loan (commonly offered by credit unions and CDFIs) reports "
                    "monthly payment history and is a standard, low-risk way to establish one."
                ),
            }
        )

    if "installment" not in account_types and len(active_tradelines) >= 1:
        suggestions.append(
            {
                "suggestion_id": "build_add_installment",
                "category": "add",
                "factor": ScoreFactor.credit_mix,
                "impact_tier": impact_tier_for_factor(ScoreFactor.credit_mix, "medium"),
                "title": "Add an installment account to diversify credit mix",
                "description": (
                    "This file has no installment loan (auto, personal, student, or credit-builder "
                    "loan) on record. A mix of revolving and installment accounts is one of the "
                    "smaller scoring factors, but a credit-builder loan is a common, low-risk way "
                    "to add one."
                ),
            }
        )

    utilization = _revolving_utilization(active_tradelines)
    if utilization is not None and utilization > UTILIZATION_TARGET:
        suggestions.append(
            {
                "suggestion_id": "build_paydown_utilization",
                "category": "add",
                "factor": ScoreFactor.amounts_owed,
                "impact_tier": impact_tier_for_factor(ScoreFactor.amounts_owed, "high"),
                "title": f"Pay down revolving balances (currently ~{utilization * 100:.0f}% utilization)",
                "description": (
                    f"Combined revolving utilization across this file is roughly {utilization * 100:.0f}%, "
                    f"above the commonly cited {int(UTILIZATION_TARGET * 100)}% guideline. Amounts Owed is "
                    "30% of a FICO Score, so utilization is usually the highest-leverage lever available "
                    "on an existing file."
                ),
            }
        )

    age_years = _oldest_account_age_years(active_tradelines)
    if age_years is not None and age_years < SHORT_HISTORY_YEARS:
        suggestions.append(
            {
                "suggestion_id": "build_lengthen_history",
                "category": "add",
                "factor": ScoreFactor.length_of_history,
                "impact_tier": impact_tier_for_factor(ScoreFactor.length_of_history, "medium"),
                "title": "Keep accounts open and consider rent/utility reporting",
                "description": (
                    f"The oldest account on this file is roughly {age_years:.1f} years old. Length of "
                    "credit history can't be sped up directly, but keeping existing accounts open, "
                    "avoiding unnecessary closures, and using a rent/utility payment reporting service "
                    "(which adds otherwise-unreported positive payment history) are the standard ways "
                    "to work with this factor over time. Becoming an authorized user on an account "
                    "belonging to someone you actually know and trust can help too -- paying a stranger "
                    "for authorized-user status on their account is not recommended; it's under "
                    "increasing regulatory scrutiny and many current scoring models are built to "
                    "discount it."
                ),
            }
        )

    return suggestions
