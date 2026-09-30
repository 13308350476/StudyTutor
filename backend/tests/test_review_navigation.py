"""Reviewing backwards restores draft/results without counting twice."""

import sys
from pathlib import Path

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend"
sys.path.insert(0, str(FRONTEND_ROOT))

from shared.review_session import result_at, review_summary  # noqa: E402


def test_back_and_forward_restores_each_question_without_duplicating_counts():
    items = [{"wrong_id": 11}, {"wrong_id": 12}, {"wrong_id": 13}]
    results = {11: {"graded": True, "is_correct": True}}
    drafts = {12: "我的综合题草稿"}
    assert result_at(items, 1, results, set()) == (None, False)
    assert drafts[12] == "我的综合题草稿"

    result, answered = result_at(items, 0, results, set())
    assert answered and result == results[11]
    results[12] = {"graded": False, "attempt_token": "pending"}
    assert result_at(items, 1, results, set()) == (results[12], True)
    assert review_summary(results, set()) == {"correct": 1, "wrong": 0, "ungraded": 1, "manual": 0}

    # Revisiting a question just reads the original result, never adds a count.
    assert result_at(items, 0, results, set()) == (results[11], True)
    assert review_summary(results, set())["correct"] == 1
    results[12] = {"graded": True, "is_correct": False, "assessment_source": "self_assessed"}
    assert review_summary(results, set()) == {"correct": 1, "wrong": 1, "ungraded": 0, "manual": 0}

    mastered = {13}
    assert result_at(items, 2, results, mastered) == (None, True)
    assert result_at(items, len(items), results, mastered) == (None, False)
    assert review_summary(results, mastered) == {"correct": 1, "wrong": 1, "ungraded": 0, "manual": 1}


def test_manual_mastery_supersedes_a_pending_ungraded_result():
    items = [{"wrong_id": 21}]
    results = {21: {"graded": False, "attempt_token": "pending"}}
    assert result_at(items, 0, results, {21}) == (results[21], True)
    assert review_summary(results, {21}) == {"correct": 0, "wrong": 0, "ungraded": 0, "manual": 1}