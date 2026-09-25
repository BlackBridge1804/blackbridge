"""
Runs the full rule set against every tradeline on a report and returns
plain-dict findings. The caller (app/routers/reports.py) is responsible for
turning these into Violation rows.

Kept independent of the database/ORM on purpose -- these functions take
plain objects with the right attributes, which is what makes them unit
testable without spinning up a database (see tests/test_rules_engine.py).
"""
from .metro2_rules import TRADELINE_RULES, rule_duplicate_collection_reporting


def scan_tradelines(tradelines: list) -> list[dict]:
    """tradelines: list of objects with the Tradeline attributes (ORM rows or
    SimpleNamespace stand-ins both work). Returns a flat list of violation dicts,
    each with at least: rule_id, legal_basis, severity, description, letter_type,
    and '_tradeline_id' identifying which tradeline it applies to."""
    findings: list[dict] = []

    for tradeline in tradelines:
        for rule in TRADELINE_RULES:
            result = rule(tradeline)
            if result:
                result = dict(result)
                result["_tradeline_id"] = tradeline.id
                findings.append(result)

    findings.extend(rule_duplicate_collection_reporting(tradelines))

    return findings


def summarize(findings: list[dict]) -> dict:
    """Category counts for the FREE-tier summary view -- no letter content, just counts."""
    by_type: dict = {}
    for f in findings:
        by_type[f["rule_id"]] = by_type.get(f["rule_id"], 0) + 1
    return {
        "violation_count": len(findings),
        "violations_by_type": by_type,
    }
