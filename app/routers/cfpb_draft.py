"""
CFPB complaint helper.

Builds a ready-to-edit complaint draft for each company whose dispute went
unanswered, was "verified", or was recorded as no response. Everything comes
from letters and outcomes already in the app. The client files the complaint
THEMSELVES at consumerfinance.gov/complaint; this app never submits anything.

The CFPB takes one complaint per company, so the draft is split by company.
Issue names below are suggestions: the live form's wording changes, so the
client picks the closest option on the form.

Draft for attorney review. Not legal advice.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_client

CFPB_URL = "https://www.consumerfinance.gov/complaint/"
STANDARD_WAIT_DAYS = 30   # FCRA 611: generally 30 days to investigate
BLOCK_WAIT_DAYS = 14      # FCRA 605B: 4 business days after receipt, plus mail time
MAX_NARRATIVE_CHARS = 4500  # the form's text box is limited; stay under it
_ITEM_TEXT_CAP = 240

_FURNISHER_TYPES = {"fcra_623_direct", "tila_1666b"}
_COLLECTOR_TYPES = {"fdcpa_809"}

CHECKLIST = [
    "I really did mail each letter listed. Where the draft shows a date that is not the day I mailed it, I fixed it.",
    "I have my proof of mailing (certified mail receipt or tracking number).",
    "I have not received a written answer from this company for the items listed, or the answer I got did not fix the problem.",
    "I will not type my full Social Security number or full account numbers into the narrative.",
    "I will file this myself on the CFPB website and keep a copy of the complaint number.",
]


def _day(d: Optional[datetime]) -> str:
    return d.date().isoformat() if d else "an unknown date"


def _cap(text: str, n: int = _ITEM_TEXT_CAP) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _company_for(letter: models.DisputeLetter, tl: models.Tradeline) -> tuple[str, str]:
    """(company name, kind) where kind is bureau / furnisher / collector."""
    if letter.letter_type in _COLLECTOR_TYPES:
        return (tl.creditor_name or letter.recipient or "[collection agency name]"), "collector"
    if letter.letter_type in _FURNISHER_TYPES:
        return (tl.creditor_name or letter.recipient or "[creditor name]"), "furnisher"
    if tl.reported_by:
        return tl.reported_by, "bureau"
    return "[Equifax, Experian or TransUnion: use the one that reported this]", "bureau"


def _issue_hints(kind: str, reasons: set, is_block: bool) -> tuple[str, str, str]:
    """(product, issue, sub_issue) suggestions."""
    if kind == "collector":
        return ("Debt collection", "Written notification about debt / problem with the debt validation", "Choose the closest option")
    if is_block:
        return ("Credit reporting", "Incorrect information on your report", "Information belongs to someone else")
    if kind == "furnisher":
        return ("Credit reporting", "Problem with a company's investigation into an existing problem", "Choose the closest option")
    sub = "Their investigation did not fix an error on your report" if "verified" in reasons else "Was not notified of investigation status or results"
    return ("Credit reporting", "Problem with a credit reporting company's investigation into an existing problem", sub)


def _eligible(letter: models.DisputeLetter, now: datetime) -> Optional[dict]:
    """Returns {reason, days, window} when this letter has reached the point of a
    complaint, else None. Deleted and updated outcomes are never complaints."""
    days = max(0, (now - letter.generated_at).days) if letter.generated_at else 0
    window = BLOCK_WAIT_DAYS if letter.letter_type == "fcra_605b" else STANDARD_WAIT_DAYS
    out = letter.outcome_status
    if out == "verified":
        return {"reason": "verified", "days": days, "window": window}
    if out == "no_response":
        return {"reason": "no_response", "days": days, "window": window}
    if out is None and days >= window:
        return {"reason": "silent", "days": days, "window": window}
    return None


def _item_line(letter, tl, v, info) -> str:
    when = _day(letter.generated_at)
    acct = f"{tl.creditor_name or 'an account'}" + (f" (account ending {tl.account_number_last4})" if tl.account_number_last4 else "")
    if letter.letter_type == "fcra_605b":
        asked = f"On or about {when} I asked for {acct} to be blocked because it resulted from identity theft (FCRA 605B), and sent my FTC identity theft report with proof of my identity."
    else:
        asked = f"On or about {when} I sent a written dispute about {acct} under {v.legal_basis}. I disputed: {_cap(v.description)}"
    if info["reason"] == "verified":
        result = "The company reported the item as 'verified' and did not correct it."
    elif info["reason"] == "no_response":
        result = "I did not receive a response."
    else:
        result = f"It has been {info['days']} days and I have not received a written result."
    return f"- {asked} [CONFIRM THE DATE YOU REALLY MAILED IT.] {result}"


def build_draft(db: Session, report: models.Report, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    rows = (
        db.query(models.DisputeLetter, models.Violation, models.Tradeline)
        .join(models.Violation, models.DisputeLetter.violation_id == models.Violation.id)
        .join(models.Tradeline, models.Violation.tradeline_id == models.Tradeline.id)
        .filter(models.Tradeline.report_id == report.id)
        .order_by(models.DisputeLetter.generated_at.asc())
        .all()
    )

    groups: dict = {}
    not_yet = []
    for letter, v, tl in rows:
        info = _eligible(letter, now)
        company, kind = _company_for(letter, tl)
        if info is None:
            if letter.outcome_status is None and letter.generated_at:
                wait = BLOCK_WAIT_DAYS if letter.letter_type == "fcra_605b" else STANDARD_WAIT_DAYS
                left = max(0, wait - (now - letter.generated_at).days)
                not_yet.append({
                    "company": company,
                    "creditor_name": tl.creditor_name,
                    "sent_on": _day(letter.generated_at),
                    "days_left": left,
                })
            continue
        g = groups.setdefault(company, {"kind": kind, "items": [], "reasons": set(), "block": True})
        g["items"].append((letter, tl, v, info))
        g["reasons"].add(info["reason"])
        if letter.letter_type != "fcra_605b":
            g["block"] = False

    complaints = []
    for company, g in groups.items():
        product, issue, sub = _issue_hints(g["kind"], g["reasons"], g["block"])
        lines = [_item_line(l, t, v, i) for (l, t, v, i) in g["items"]]
        if g["block"]:
            law = ("Section 605B of the Fair Credit Reporting Act requires a block of information "
                   "resulting from identity theft no later than 4 business days after the company "
                   "receives the request and the required documents.")
            resolution = ("Block the information listed above from my file as required by law, "
                          "and send me written confirmation.")
        else:
            law = ("The Fair Credit Reporting Act generally requires a reasonable investigation "
                   "and a written result within 30 days of a dispute.")
            resolution = ("Complete a reasonable investigation. Delete or correct the item(s) listed "
                          "above if they cannot be verified as accurate, and send me the results in writing, "
                          "including how the item was verified.")
        head = f"I am filing this complaint about {company}."
        narrative = "\n\n".join([head, "What happened:\n" + "\n".join(lines), law])
        while len(narrative) > MAX_NARRATIVE_CHARS and len(lines) > 1:
            lines.pop()  # keep the oldest items; mention the rest
            narrative = "\n\n".join([head, "What happened:\n" + "\n".join(lines) +
                                     "\n- Other items from my letters are also unresolved; I can provide details.", law])
        complaints.append({
            "company": company,
            "company_kind": g["kind"],
            "product": product,
            "issue": issue,
            "sub_issue": sub,
            "item_count": len(g["items"]),
            "narrative": narrative,
            "narrative_chars": len(narrative),
            "resolution": resolution,
        })

    if complaints:
        message = (f"{len(complaints)} complaint draft(s) ready. The CFPB takes one complaint per company, "
                   "so file each one separately.")
    elif not_yet:
        message = "Nothing is ready yet. Your letters are still inside the response window. Check back after the dates below."
    elif not rows:
        message = "No dispute letters yet. Generate your letters first; a complaint comes after a company misses its deadline or verifies the item."
    else:
        message = "Nothing needs a complaint right now. Every letter with an outcome was deleted or updated."

    return {
        "report_id": report.id,
        "generated_on": now.date().isoformat(),
        "ready": bool(complaints),
        "message": message,
        "complaints": complaints,
        "not_yet": not_yet,
        "checklist": CHECKLIST,
        "cfpb_url": CFPB_URL,
    }


router = APIRouter(tags=["cfpb draft"])


@router.get("/cfpb/draft")
def cfpb_draft(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    from .letters import _get_owned_paid_report  # late import: letters.py mounts this module

    report = _get_owned_paid_report(db, report_id, client)
    return build_draft(db, report)
