"""Small SQLite schema syncs for local-first runtime upgrades."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


MISCONCEPTION_COLUMNS = {
    "error_cause": "TEXT",
    "confused_concepts_json": "TEXT",
    "correct_reasoning_path": "TEXT",
    "recommended_actions_json": "TEXT",
    "related_knowledge_tag": "VARCHAR(200)",
    "analysis_confidence": "FLOAT",
    "analysis_model": "VARCHAR(100)",
    "analysis_source": "VARCHAR(50)",
}

WEAK_KNOWLEDGE_COLUMNS = {
    "subject": "VARCHAR(64)",
    "chapter": "VARCHAR(128)",
    "ai_summary": "TEXT",
    "recommended_actions_json": "TEXT",
}

QUIZ_RECORD_COLUMNS = {
    "assessment_source": "VARCHAR(20) NOT NULL DEFAULT 'auto'",
}

REVIEW_SCHEDULE_COLUMNS = {
    "review_stage": "INTEGER NOT NULL DEFAULT 0",
    "next_review_at": "DATETIME",
    "mastery_source": "VARCHAR(16)",
}


def _add_missing_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns(table)}
    missing = [
        (name, ddl_type) for name, ddl_type in columns.items() if name not in existing
    ]
    if not missing:
        return

    with engine.begin() as conn:
        for name, ddl_type in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


def sync_agent_knowledge_schema(engine: Engine) -> None:
    """Add missing columns to existing local SQLite databases."""
    _add_missing_columns(engine, "misconceptions", MISCONCEPTION_COLUMNS)
    _add_missing_columns(engine, "weak_knowledge", WEAK_KNOWLEDGE_COLUMNS)
    _add_missing_columns(engine, "quiz_records", QUIZ_RECORD_COLUMNS)
    has_wrong_questions = inspect(engine).has_table("wrong_questions")
    old_columns = ({column["name"] for column in inspect(engine).get_columns("wrong_questions")}
                   if has_wrong_questions else set())
    had_schedule = "next_review_at" in old_columns
    _add_missing_columns(engine, "wrong_questions", REVIEW_SCHEDULE_COLUMNS)
    if has_wrong_questions:
        with engine.begin() as conn:
            if not had_schedule:
                # On a pre-scheduling database, already reviewed questions
                # retain a conservative one-day interval from last activity.
                conn.execute(text("""
                    UPDATE wrong_questions
                    SET next_review_at = datetime(
                        COALESCE(last_review_at, added_at), '+1 day'
                    )
                    WHERE next_review_at IS NULL AND review_count > 0
                """))
            # First reviews are due one day after collection. Convert only
            # uncompleted same-day schedules from the temporary 0-day rule;
            # leave already reviewed and manually mastered rows untouched.
            # Idempotent: after conversion the due date differs from added_at.
            conn.execute(text("""
                UPDATE wrong_questions
                SET next_review_at = datetime(added_at, '+1 day')
                WHERE review_count = 0 AND last_review_at IS NULL
                    AND last_status != 'correct'
                    AND (next_review_at IS NULL OR date(next_review_at) = date(added_at))
            """))
            # Under the old 0-day rule a question could be practiced first,
            # then scheduled again for the same day. Do not skip these merely
            # because they already have a review_count. Keep their stage and
            # any genuinely later dates unchanged; this is idempotent.
            conn.execute(text("""
                UPDATE wrong_questions
                SET next_review_at = datetime(next_review_at, '+1 day')
                WHERE review_count > 0 AND last_review_at IS NOT NULL
                    AND review_stage = 0 AND last_status != 'correct'
                    AND next_review_at IS NOT NULL
                    AND date(next_review_at) = date(last_review_at)
            """))
            if "mastery_source" not in old_columns:
                # Old 'correct' meant one correct review, NOT graduation from
                # the 14-day stage. Keep their existing stage and next date.
                conn.execute(text("""
                    UPDATE wrong_questions
                    SET last_status = 'reviewing',
                        next_review_at = COALESCE(
                            next_review_at, datetime(
                                COALESCE(last_review_at, added_at), '+1 day'
                            )
                        )
                    WHERE last_status = 'correct' AND review_count > 0
                """))
