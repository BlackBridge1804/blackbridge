"""
Deterministic checks for when a violation has crossed from "dispute it" into
"this might be worth an attorney's time." Same philosophy as the rules
engine: every flag traces back to a specific, checkable fact (an outcome
someone recorded), never a guess.

IMPORTANT: flagging something here is not a legal conclusion and not legal
advice -- it's a triage signal for a human (ideally a partner attorney) to
review. See the strategy doc's "Litigation referral" section for why this
still needs attorney sign-off before it's built into a real revenue feature,
and the state-bar fee-splitting caveat that goes with it.
"""
from datetime import datetime
from typing import Optional

# If a dispute letter has had no outcome recorded this long after being sent,
# treat "silence" as itself worth flagging -- FCRA requires a substantive
# response within roughly 30 days.
NO_RESPONSE_THRESHOLD_DAYS = 45


def _days_since(generated_at, now: Optional[datetime] = None) -> float:
    now = now or datetime.utcnow()
    if generated_at is None:
        return 0
    return (now - generated_at).total_seconds() / 86400


def is_litigation_candidate(letters: list) -> Optional[dict]:
    """letters: every DisputeLetter (or equivalent object) tied to ONE violation,
    in any order. Returns a reason dict if this violation looks litigation-worthy,
    else None.

    Flags on either of two fact patterns:
      1. The original dispute came back "verified," AND a Method-of-Verification
         escalation also came back "verified" (or unanswered past the threshold)
         -- i.e. the furnisher/bureau couldn't or wouldn't show real work twice.
      2. Any dispute letter has had no outcome recorded well past FCRA's ~30-day
         response window -- a non-response is itself a potential violation.
    """
    originals = [l for l in letters if not getattr(l, "escalates_letter_id", None)]
    escalations = {l.escalates_letter_id: l for l in letters if getattr(l, "escalates_letter_id", None)}

    for original in originals:
        if original.outcome_status == "verified":
            mov = escalations.get(original.id)
            if mov is not None:
                if mov.outcome_status == "verified":
                    return {
                        "reason": "mov_still_verified",
                        "detail": (
                            "The original dispute was reported 'verified,' and the follow-up "
                            "Method of Verification request was also reported 'verified' -- "
                            "worth a real attorney's review of whether a substantive "
                            "investigation actually occurred."
                        ),
                    }
                if mov.outcome_status is None and _days_since(mov.generated_at) > NO_RESPONSE_THRESHOLD_DAYS:
                    return {
                        "reason": "mov_no_response",
                        "detail": (
                            f"A Method of Verification request has gone unanswered for over "
                            f"{NO_RESPONSE_THRESHOLD_DAYS} days after an initial 'verified' outcome."
                        ),
                    }

        if original.outcome_status is None and _days_since(original.generated_at) > NO_RESPONSE_THRESHOLD_DAYS:
            return {
                "reason": "original_no_response",
                "detail": (
                    f"No outcome has been recorded for this dispute more than "
                    f"{NO_RESPONSE_THRESHOLD_DAYS} days after it was sent -- FCRA generally "
                    "requires a substantive response within about 30 days."
                ),
            }

    return None
