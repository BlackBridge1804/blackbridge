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


_RENTAL_KEYWORDS = (
    "apartment", "apartments", "property management", "realty", "rental",
    "leasing", "residential", "housing authority",
)


def rule_collection_validation_eligible(tradeline) -> Optional[dict]:
    """Every collection-agency tradeline is eligible for an FDCPA Section 809
    validation request. IMPORTANT: Section 809(b)'s automatic "cease collection
    activity" requirement only applies if the request is sent within 30 days of
    the collector's first written notice to the consumer -- a credit report has
    no way to know that date, so this rule (and the letter it maps to) never
    asserts the cessation right unconditionally. The underlying right to
    request validation itself does not expire."""
    if tradeline.is_collection:
        return {
            "rule_id": "fdcpa_validation_eligible",
            "legal_basis": "FDCPA Section 809 (15 U.S.C. Section 1692g)",
            "severity": "medium",
            "description": (
                f"'{tradeline.creditor_name or 'This account'}' is being reported by a collection "
                f"agency. You have the right to demand validation of this debt under FDCPA Section "
                f"809 -- that right doesn't expire, though the automatic requirement that the "
                f"collector pause collection activity while validating only applies if this is sent "
                f"within 30 days of the collector's first written notice to you."
            ),
            "letter_type": "fdcpa_809",
        }
    return None


def rule_repossession_accuracy(tradeline) -> Optional[dict]:
    """Doesn't allege one specific error -- flags a repossession tradeline as
    worth a Section 611 reinvestigation covering the fields that are commonly
    reported incompletely or inconsistently on repos: the sale date, and the
    post-sale deficiency-balance calculation (what's left owed after sale
    proceeds were applied)."""
    status = (tradeline.status_text or "").lower()
    if "repossess" in status:
        return {
            "rule_id": "fcra_repossession_accuracy",
            "legal_basis": "FCRA Section 611 (15 U.S.C. Section 1681i)",
            "severity": "medium",
            "description": (
                f"'{tradeline.creditor_name or 'This account'}' is reported as repossessed. "
                f"Repossession tradelines are commonly reported with an inaccurate or missing sale "
                f"date, or an unverifiable deficiency balance (the amount still owed after sale "
                f"proceeds were applied) -- both of which the furnisher must be able to verify under "
                f"FCRA Section 611."
            ),
            "letter_type": "fcra_611",
        }
    return None


def rule_possible_rental_collection(tradeline) -> Optional[dict]:
    """Heuristic only, matched on creditor-name keywords -- can both miss real
    rental collections (a debt buyer with a generic name) and occasionally
    false-positive. Evictions themselves generally do NOT appear on the big
    three credit reports; they live on separate tenant-screening consumer
    reports (e.g. LexisNexis RentBureau, SafeRent), a different kind of CRA
    entirely. What sometimes does appear here is an unpaid-rent balance sent
    to collections -- that's what this rule looks for, and the letter it maps
    to is worded as an ordinary furnisher-accuracy dispute, never an assertion
    that the account is definitely eviction-related."""
    if not tradeline.is_collection:
        return None
    haystack = " ".join(filter(None, [tradeline.creditor_name, tradeline.original_creditor_name])).lower()
    if any(keyword in haystack for keyword in _RENTAL_KEYWORDS):
        return {
            "rule_id": "fcra_rental_collection_furnisher_accuracy",
            "legal_basis": "FCRA Section 623 (15 U.S.C. Section 1681s-2)",
            "severity": "medium",
            "description": (
                f"'{tradeline.creditor_name or 'This collection account'}' looks like it may be a "
                f"rental/property-management-related collection (possibly tied to unpaid rent or an "
                f"eviction judgment), based on the creditor name alone -- verify that before relying "
                f"on it. If it is rental-related, the furnisher still has the same Section 623 duty "
                f"to report accurate, verifiable information as any other furnisher. Note: an "
                f"eviction itself typically won't appear on this report -- it lives on a separate "
                f"tenant-screening consumer report, not Equifax/Experian/TransUnion."
            ),
            "letter_type": "fcra_623_direct",
        }
    return None


# Per-tradeline rules, run against every tradeline individually.
TRADELINE_RULES = [
    rule_missing_dofd_on_delinquent_account,
    rule_date_closed_before_date_opened,
    rule_balance_exceeds_credit_limit,
    rule_obsolete_negative_item,
    rule_status_balance_contradiction,
    rule_collection_validation_eligible,
    rule_repossession_accuracy,
    rule_possible_rental_collection,
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
