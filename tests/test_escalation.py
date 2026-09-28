"""
Unit tests for app/escalation.py -- the litigation-candidate triage logic.
Uses lightweight stand-in objects (SimpleNamespace) rather than real
DisputeLetter ORM rows, same pattern as the rules-engine tests.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.escalation import NO_RESPONSE_THRESHOLD_DAYS, is_litigation_candidate


def _letter(id_, outcome_status=None, escalates_letter_id=None, days_ago=0):
    return SimpleNamespace(
        id=id_,
        outcome_status=outcome_status,
        escalates_letter_id=escalates_letter_id,
        generated_at=datetime.utcnow() - timedelta(days=days_ago),
    )


def test_no_flag_when_dispute_is_fresh_and_pending():
    letters = [_letter("l1", outcome_status=None, days_ago=5)]
    assert is_litigation_candidate(letters) is None


def test_no_flag_when_original_deleted():
    letters = [_letter("l1", outcome_status="deleted", days_ago=40)]
    assert is_litigation_candidate(letters) is None


def test_flags_when_mov_escalation_also_verified():
    letters = [
        _letter("l1", outcome_status="verified", days_ago=60),
        _letter("l2", outcome_status="verified", escalates_letter_id="l1", days_ago=20),
    ]
    result = is_litigation_candidate(letters)
    assert result is not None
    assert result["reason"] == "mov_still_verified"


def test_flags_when_mov_escalation_unanswered_past_threshold():
    letters = [
        _letter("l1", outcome_status="verified", days_ago=90),
        _letter("l2", outcome_status=None, escalates_letter_id="l1", days_ago=NO_RESPONSE_THRESHOLD_DAYS + 1),
    ]
    result = is_litigation_candidate(letters)
    assert result is not None
    assert result["reason"] == "mov_no_response"


def test_does_not_flag_mov_unanswered_within_threshold():
    letters = [
        _letter("l1", outcome_status="verified", days_ago=10),
        _letter("l2", outcome_status=None, escalates_letter_id="l1", days_ago=5),
    ]
    assert is_litigation_candidate(letters) is None


def test_flags_original_with_no_response_past_threshold():
    letters = [_letter("l1", outcome_status=None, days_ago=NO_RESPONSE_THRESHOLD_DAYS + 5)]
    result = is_litigation_candidate(letters)
    assert result is not None
    assert result["reason"] == "original_no_response"
