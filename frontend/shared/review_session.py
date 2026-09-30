"""In-memory state for navigating one wrong-question review batch."""


def result_at(items: list[dict], index: int, results: dict, manually_mastered: set) -> tuple[dict | None, bool]:
    """Restore one question without triggering another submission."""
    if index < 0 or index >= len(items):
        return None, False
    wrong_id = items[index]["wrong_id"]
    result = results.get(wrong_id)
    return result, result is not None or wrong_id in manually_mastered


def review_summary(results: dict, manually_mastered: set) -> dict[str, int]:
    """Count each question once; a manual override is never a correct answer."""
    counts = {"correct": 0, "wrong": 0, "ungraded": 0, "manual": len(manually_mastered)}
    for wrong_id, result in results.items():
        if wrong_id in manually_mastered:
            continue
        if not result.get("graded", False):
            counts["ungraded"] += 1
        elif result.get("is_correct"):
            counts["correct"] += 1
        else:
            counts["wrong"] += 1
    return counts