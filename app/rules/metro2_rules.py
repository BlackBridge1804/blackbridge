"""
Deterministic violation-detection rules.

DESIGN RULE: every function here returns a plain dict (or None / a list of
dicts) built entirely from fields already on the tradeline -- no model
inference, no judgment calls, nothing that isn't traceable back to a specific
field value. That's what makes every claim in a generated letter auditable.

This is a starting rule set covering the categories described in the
strategy doc (Metro 2 field violations, obsolescence, re-aging, duplicate
reporting, status contradictions). Extend it -- but keep every new rule this
same shape: read fields in, return a fact-based finding out, no free text
generation here (that happens later, in app/letters/generator.py).

None of this is legal advice; a CROA/consumer-finance attorney should review
the rule set and every letter template before this goes anywhere near a real
consumer's dispute.
"""
from datetime import date, datetime
from typing import Optional

OBSOLESCENCE_YEARS = 7  # FCRA general rule; some records (e.g. certain bankruptcies) run longer -- verify per item


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def rule_missing_dofd_on_delinquent_account(tradeline) -> Optional[dict]:
    """A collection/charged-off account with no Date of First Delinquency is a Metro 2
    field violation: DOFD is a required field once an account is delinquent, and its
    absence makes it impossible to verify the account isn't past the reporting window."""
    is_derogatory = tradeline.is_collection or (
        tradeline.status_text and "charge" in tradeline.status_text.lower()
    )
    if is_derogatory and not tradeline.date_of_first_delinquency:
        return {
            "rule_id": "metro2_missing_dofd",
            "legal_basis": "Metro 2 field specification; FCRA 623(a)(5) furnisher duty on delinquency dates",
            "severity": "high",
            "description": (
                f"Tradeline for {tradeline.creditor_name or 'this creditor'} is reported as "
                f"derogatory (collection/charge-off) but has no Date of First Delinquency on file. "
                f"DOFD is required to verify the account is within the reporting window."
            ),
            "letter_type": "fcra_623_direct",
        }
    return None


def rule_date_closed_before_date_opened(tradeline) -> Optional[dict]:
    opened = _parse_date(tradeline.date_opened)
    closed = _parse_date(tradeline.date_closed)
    if opened and closed and closed < opened:
        return {
            "rule_id": "metro2_date_sequence_error",
            "legal_basis": "Metro 2 field specification (internally inconsistent dates); FCRA 611 accuracy",
            "severity": "high",
            "description": (
                f"Tradeline for {tradeline.creditor_name or 'this creditor'} shows a Date Closed "
                f"({tradeline.date_closed}) earlier than its Date Opened ({tradeline.date_opened}), "
                f"which is internally impossible and indicates a data error."
            ),
            "letter_type": "fcra_611",
        }
    return None


def rule_balance_exceeds_credit_limit(tradeline) -> Optional[dict]:
    if (
        tradeline.account_type
        and tradeline.account_type.lower() == "revolving"
        and tradeline.credit_limit
        and tradeline.balance
        and tradeline.balance > tradeline.credit_limit
    ):
        return {
            "rule_id": "metro2_balance_exceeds_limit",
            "legal_basis": "Metro 2 field specification; FCRA 611 accuracy",
            "severity": "medium",
            "description": (
                f"Revolving tradeline for {tradeline.creditor_name or 'this creditor'} reports a balance "
                f"of {tradeline.balance / 100:.2f} against a credit limit of {tradeline.credit_limit / 100:.2f} "
                f"with no over-limit explanation on file -- worth verifying as a possible reporting error."
            ),
            "letter_type": "fcra_611",
        }
    return None


def rule_obsolete_negative_item(tradeline, today: Optional[date] = None) -> Optional[dict]:
    today = today or date.today()
    dofd = _parse_date(tradeline.date_of_first_delinquency)
    is_derogatory = tradeline.is_collection or (
        tradeline.status_text and "charge" in tradeline.status_text.lower()
    )
    if is_derogatory and dofd:
        years_reporting = (today - dofd).days / 365.25
        if years_reporting > OBSOLESCENCE_YEARS:
            return {
                "rule_id": "fcra_obsolescence",
                "legal_basis": "FCRA 605(a) obsolescence (7-year reporting limit)",
                "severity": "high",
                "description": (
                    f"Tradeline for {tradeline.creditor_name or 'this creditor'} has a Date of First "
                    f"Delinquency of {tradeline.date_of_first_delinquency}, roughly {years_reporting:.1f} "
                    f"years ago -- past FCRA's general 7-year reporting window and should have aged off."
                ),
                "letter_type": "fcra_611",
            }
    return None


_ZERO_BALANCE_IMPLIED_STATUSES = ("paid in full", "settled in full", "paid and closed", "zero balance")


def rule_status_balance_contradiction(tradeline) -> Optional[dict]:
    """Only flags statuses that specifically imply a zero balance. 'Paid as
    agreed' is NOT one of these -- it describes payment history on an account
    that can still be open and carrying a normal balance, so treating it as a
    contradiction would be a false positive. Precision matters more than
    recall here: every finding has to survive scrutiny in a real dispute."""
    status = (tradeline.status_text or "").lower()
    implies_zero_balance = any(phrase in status for phrase in _ZERO_BALANCE_IMPLIED_STATUSES)
    if implies_zero_balance and tradeline.balance and tradeline.balance > 0:
        return {
            "rule_id": "metro2_status_balance_contradiction",
            "legal_basis": "Metro 2 field specification; FCRA 611 accuracy",
            "severity": "medium",
            "description": (
                f"Tradeline for {tradeline.creditor_name or 'this creditor'} is marked "
                f"'{tradeline.status_text}' but still reports a balance of {tradeline.balance / 100:.2f}, "
                f"which is internally inconsistent."
            ),
            "letter_type": "fcra_611",
        }
    return None


# Per-tradeline rules, run against every tradeline individually.
TRADELINE_RULES = [
    rule_missing_dofd_on_delinquent_account,
    rule_date_closed_before_date_opened,
    rule_balance_exceeds_credit_limit,
    rule_obsolete_negative_item,
    rule_status_balance_contradiction,
]


def rule_duplicate_collection_reporting(tradelines: list) -> list[dict]:
    """Cross-tradeline check: the same debt showing once from the original creditor and
    again from a collection agency at the same time is a common duplicate-reporting
    violation. Matched here on original_creditor_name against another line's creditor_name."""
    findings = []
    collections = [t for t in tradelines if t.is_collection and t.original_creditor_name]
    originals = {
        (t.creditor_name or "").strip().lower(): t
        for t in tradelines
        if not t.is_collection and t.creditor_name
    }
    for collection in collections:
        key = (collection.original_creditor_name or "").strip().lower()
        original = originals.get(key)
        if original is not None:
            findings.append(
                {
                    "rule_id": "fcra_duplicate_reporting",
                    "legal_basis": "FCRA 611 accuracy; Metro 2 duplicate-account guidance",
                    "severity": "high",
                    "description": (
                        f"'{collection.original_creditor_name}' appears to be reported twice: once "
                        f"directly and once via a collection agency ({collection.creditor_name}) for "
                        f"what looks like the same underlying debt. Only one tradeline should typically "
                        f"remain once a debt is placed for collection."
                    ),
                    "letter_type": "fcra_611",
                    "_tradeline_id": collection.id,
                }
            )
    return findings
