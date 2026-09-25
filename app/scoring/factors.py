"""
FICO's own published score-factor categories and weights (see
https://www.myfico.com/credit-education/whats-in-your-credit-score). These
are the real, publicly documented weights -- not something we invented.

DELIBERATE LIMITATION: this module tags findings with a qualitative impact
tier (High/Medium/Low), never a specific point-number prediction. Even
myFICO's own official simulator -- built by FICO, on the real proprietary
model -- heavily caveats its published ranges ("results may vary beyond
what's in the table") because impact depends on a consumer's whole profile,
not one action in isolation. A third party without the real model making a
confident personalized point claim ("your score could go up 50-100 points")
is exactly the kind of unsupportable, misleading statement CROA prohibits.
If real point estimates matter to the product later, license an actual
simulator from FICO/VantageScore rather than approximating one here.
"""
import enum


class ScoreFactor(str, enum.Enum):
    payment_history = "payment_history"
    amounts_owed = "amounts_owed"
    length_of_history = "length_of_history"
    new_credit = "new_credit"
    credit_mix = "credit_mix"


FICO_FACTOR_WEIGHTS = {
    ScoreFactor.payment_history: 0.35,
    ScoreFactor.amounts_owed: 0.30,
    ScoreFactor.length_of_history: 0.15,
    ScoreFactor.new_credit: 0.10,
    ScoreFactor.credit_mix: 0.10,
}

FACTOR_LABELS = {
    ScoreFactor.payment_history: "Payment History (35% of a FICO Score)",
    ScoreFactor.amounts_owed: "Amounts Owed / Utilization (30% of a FICO Score)",
    ScoreFactor.length_of_history: "Length of Credit History (15% of a FICO Score)",
    ScoreFactor.new_credit: "New Credit (10% of a FICO Score)",
    ScoreFactor.credit_mix: "Credit Mix (10% of a FICO Score)",
}

# Every rules-engine violation maps to the factor it's most directly evidence
# of being wrong about. This reuses the SAME findings the dispute letters are
# built from -- no separate detection logic, just a different lens on it.
RULE_ID_TO_FACTOR = {
    "metro2_missing_dofd": ScoreFactor.payment_history,
    "metro2_date_sequence_error": ScoreFactor.length_of_history,
    "metro2_balance_exceeds_limit": ScoreFactor.amounts_owed,
    "fcra_obsolescence": ScoreFactor.payment_history,
    "metro2_status_balance_contradiction": ScoreFactor.amounts_owed,
    "fcra_duplicate_reporting": ScoreFactor.payment_history,
}


def impact_tier_for_factor(factor: ScoreFactor, severity: str = "medium") -> str:
    """Qualitative only. High-weight factors (payment history, amounts owed)
    default to a higher tier; severity from the rules engine can push it up,
    never down below what the factor weight alone would justify."""
    weight = FICO_FACTOR_WEIGHTS[factor]
    base_tier = "high" if weight >= 0.30 else ("medium" if weight >= 0.15 else "low")
    if severity == "high" and base_tier == "medium":
        return "high"
    return base_tier
