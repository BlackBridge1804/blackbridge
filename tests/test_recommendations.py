"""
Unit tests for the scoring/recommendations module. The most important
property tested here isn't a specific suggestion -- it's the guardrail
described in the strategy doc: nothing in this module should ever emit a
specific numeric score-point prediction, only qualitative factor tags and
impact tiers.
"""
import re

from app.parsing.extractor import extract_from_text, tradeline_dicts_to_namespaces
from app.rules.engine import scan_tradelines
from app.scoring.build_suggestions import generate_build_suggestions
from app.scoring.recommendations import build_recommendations, summarize_recommendations
from pathlib import Path

SAMPLE_PATH = Path(__file__).parent / "sample_data" / "sample_report.txt"

# Matches things like "50-100 points", "+80 points", "raise your score 50 points" --
# a specific point-count claim, which this module must never produce.
_POINT_PREDICTION_RE = re.compile(r"\d+\s*(-|to)\s*\d+\s*points|\d+\s*points\b", re.IGNORECASE)


def _load_sample_tradelines():
    raw_text = SAMPLE_PATH.read_text()
    tradeline_dicts = extract_from_text(raw_text)
    return tradeline_dicts_to_namespaces(tradeline_dicts)


def test_build_suggestions_flags_high_utilization():
    tradelines = _load_sample_tradelines()
    suggestions = generate_build_suggestions(tradelines)
    util_suggestions = [s for s in suggestions if s["suggestion_id"] == "build_paydown_utilization"]
    # Broken Utility Co (300/200) and Big Bank Visa (450/1000) combine to
    # well above the 30% utilization guideline.
    assert len(util_suggestions) == 1


def test_recommendations_combine_remove_and_add():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    recs = build_recommendations(tradelines, findings)
    categories = {r["category"] for r in recs}
    assert "remove" in categories
    assert "add" in categories


def test_recommendations_sorted_by_impact_tier():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    recs = build_recommendations(tradelines, findings)
    tier_rank = {"high": 0, "medium": 1, "low": 2}
    ranks = [tier_rank[r["impact_tier"]] for r in recs]
    assert ranks == sorted(ranks)


def test_no_recommendation_ever_contains_a_point_number_prediction():
    """The core guardrail: qualitative tiers only, never a fabricated point count."""
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    recs = build_recommendations(tradelines, findings)
    for r in recs:
        assert not _POINT_PREDICTION_RE.search(r["description"]), (
            f"Recommendation {r['suggestion_id']} appears to contain a specific point-count "
            f"prediction, which this module must never emit: {r['description']!r}"
        )
        assert not _POINT_PREDICTION_RE.search(r["title"])


def test_summary_counts_match_full_list():
    tradelines = _load_sample_tradelines()
    findings = scan_tradelines(tradelines)
    recs = build_recommendations(tradelines, findings)
    summary = summarize_recommendations(recs)
    assert summary["total"] == len(recs)
    assert sum(summary["by_impact_tier"].values()) == len(recs)
    assert sum(summary["by_category"].values()) == len(recs)
    assert "not a prediction" in summary["disclaimer"]
