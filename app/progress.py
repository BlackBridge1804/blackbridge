"""
A deterministic, auditable "where is this report in the pipeline" summary --
same philosophy as the rules engine and the litigation-candidate triage:
every step's done/not-done state traces to a specific fact already in the
database, never a guess. This is what powers both the client's own progress
view and the licensed operator's per-client tracking (app/routers/staff.py),
so both sides of the product always see the same, consistent picture.
"""
from typing import Optional

# Fixed pipeline order. A report always moves left-to-right through these;
# nothing here is skippable, which is what makes "current stage" well-defined.
# Each step has a DONE label (past tense, shown once complete) and an
# IN-PROGRESS label (shown while it's the current, not-yet-done step) --
# using the done label for both would make "current_stage" read as if that
# step had already happened when it's actually the one being waited on.
_STEP_DEFS = [
    ("uploaded", "Report uploaded", "Report uploaded"),
    ("scanned", "Scanned for violations", "Scanning for violations"),
    ("unlocked", "Full breakdown unlocked", "Awaiting checkout"),
    ("letters_generated", "Dispute letters generated", "Ready to generate letters"),
    ("outcomes_tracked", "Bureau/furnisher responses recorded", "Awaiting bureau/furnisher responses"),
]

def compute_report_progress(
    report,
    tradeline_count: int,
    violation_count: int,
    letters: list,
    litigation_candidate_count: Optional[int] = None,
) -> dict:
    """report: a Report row. letters: every DisputeLetter tied to this report
    (across all its violations). Returns a JSON-serializable dict -- see
    schemas.ReportProgressOut for the exact shape this fills."""
    letters_with_outcome = sum(1 for l in letters if l.outcome_status)
    letters_pending_outcome = len(letters) - letters_with_outcome

    done_flags = {
        "uploaded": True,  # if we're computing this at all, the report exists
        "scanned": report.status == "scanned",
        "unlocked": bool(report.is_paid),
        "letters_generated": len(letters) > 0,
        "outcomes_tracked": len(letters) > 0 and letters_pending_outcome == 0,
    }
    in_progress_labels = {key: in_progress for key, _done_label, in_progress in _STEP_DEFS}

    steps = [
        {"key": key, "label": done_label, "done": done_flags[key]}
        for key, done_label, _in_progress in _STEP_DEFS
    ]

    current_stage = "complete"
    current_stage_label = "All steps complete"
    for step in steps:
        if not step["done"]:
            current_stage = step["key"]
            current_stage_label = in_progress_labels[step["key"]]
            break

    return {
        "report_id": report.id,
        "status": report.status,
        "created_at": report.created_at,
        "is_paid": bool(report.is_paid),
        "current_stage": current_stage,
        "current_stage_label": current_stage_label,
        "steps": steps,
        "tradeline_count": tradeline_count,
        "violation_count": violation_count,
        "letters_generated": len(letters),
        "letters_with_outcome": letters_with_outcome,
        "letters_pending_outcome": letters_pending_outcome,
        "litigation_candidates": litigation_candidate_count,
    }
