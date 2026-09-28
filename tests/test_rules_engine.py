"""
Unit tests for the rules engine -- the part of the system that has to be
right every time, since every claim in a generated letter traces back to
these findings. Run with: pytest
"""
from pathlib import Path

from app.parsing.extractor import extract_from_text, tradeline_dicts_to_namespaces
from app.rules.engine import scan_tradelines, summarize

SAMPLE_PATH = Path(__file__).parent / "sample_data" / "sample_report.txt"


def _load_sample_tradelines():
    raw_text = SAMPLE_PATH.read_text()
    tradeline_dicts = extract_from_text(raw_text)
    return tradeline_dicts_to_namespaces(tradeline_dicts)


def test_parser_extracts_all_sample_tradelines():
    tradelines = _load_sample_tradelines()
    assert len(tradelines) == 6


def test_missing_dofd_on_collection_is_flagged():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    dofd_findings = [f for f in findings if f["rule_id"] == "metro2_missing_dofd"]
    # Acme Collections is a collection account with no DOFD in the sample data.
    assert any("Acme Collections" in f["description"] for f in dofd_findings)


def test_date_closed_before_date_opened_does_not_false_positive_on_clean_dates():
    # Every account in the sample file has Date Closed on/after Date Opened,
    # so this rule should find nothing -- confirms it doesn't fire on valid data.
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    date_findings = [f for f in findings if f["rule_id"] == "metro2_date_sequence_error"]
    assert date_findings == []


def test_status_balance_contradiction_only_flags_zero_balance_implying_statuses():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    contradictions = [f for f in findings if f["rule_id"] == "metro2_status_balance_contradiction"]
    # Mixed Up Finance is "paid in full" with a $175 balance -- a real contradiction.
    assert len(contradictions) == 1
    assert "Mixed Up Finance" in contradictions[0]["description"]
    # Big Bank Visa and Broken Utility Co are "paid as agreed" with normal nonzero
    # balances on open revolving accounts -- NOT a contradiction, must not be flagged.
    assert not any("Big Bank Visa" in f["description"] for f in contradictions)
    assert not any("Broken Utility Co" in f["description"] for f in contradictions)


def test_balance_exceeds_credit_limit_is_flagged():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    over_limit = [f for f in findings if f["rule_id"] == "metro2_balance_exceeds_limit"]
    # Broken Utility Co: balance 300.00 > limit 200.00
    assert any("Broken Utility Co" in f["description"] for f in over_limit)


def test_duplicate_collection_reporting_is_flagged():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    duplicates = [f for f in findings if f["rule_id"] == "fcra_duplicate_reporting"]
    # Acme Collections reports "Old Retail Card" as the original creditor, and
    # Old Retail Card also appears as its own (non-collection) tradeline.
    assert len(duplicates) == 1
    assert "Old Retail Card" in duplicates[0]["description"]


def test_summarize_counts_match_findings():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    summary = summarize(findings)
    assert summary["violation_count"] == len(findings)
    assert sum(summary["violations_by_type"].values()) == len(findings)


def test_every_finding_has_an_auditable_legal_basis():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    assert len(findings) > 0
    for finding in findings:
        assert finding["legal_basis"], f"Finding {finding['rule_id']} has no legal basis cited"
        assert finding["letter_type"], f"Finding {finding['rule_id']} has no letter_type mapped"
