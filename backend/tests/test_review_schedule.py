"""Scheduled reviews remain due when missed and advance only on graded reviews."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database.schema_sync import sync_agent_knowledge_schema
from app.models.question import Question
from app.models.quiz_record import QuizRecord
from app.models.wrong_question import WrongQuestion
from app.repositories import wrong_question_repo
from app.services import quiz_service
from app.services.quiz_service import QuizService
from app.services.wrong_question_service import WrongQuestionService


def make_question(db, *, choice=True):
    q = Question(
        subject="数据结构", question_type="choice" if choice else "other",
        question_text="测试复习排期", answer="B" if choice else "参考解析",
        option_a="A" if choice else None,
        option_b="B" if choice else None,
    )
    db.add(q)
    db.commit()
    return q


def test_wrong_practice_schedules_tomorrow_and_graded_reviews_advance(db_session, monkeypatch):
    q = make_question(db_session)
    monkeypatch.setattr(
        "app.services.misconception_service.MisconceptionService.analyze_wrong_answer",
        lambda *args, **kwargs: None,
    )
    QuizService(db_session).submit_answer(q.id, "A")
    wrong = WrongQuestionService(db_session).repo.get_by_question_id(q.id)
    assert wrong.next_review_at.date() == (datetime.now() + timedelta(days=1)).date()
    assert wrong.review_stage == 0
    assert WrongQuestionService(db_session).get_due_reviews()["total"] == 0

    # Missing the appointment doesn't skip a stage or count as a wrong answer.
    seven_days_later = wrong.next_review_at + timedelta(days=6)
    due = WrongQuestionService(db_session).repo.get_due(now=seven_days_later)
    assert [entry.id for entry in due] == [wrong.id]
    assert wrong.review_count == 0

    current_time = [seven_days_later]

    class ReviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time[0]

    monkeypatch.setattr(wrong_question_repo, "datetime", ReviewClock)

    for expected_stage, expected_interval in [(1, 3), (2, 7), (3, 14)]:
        result = WrongQuestionService(db_session).submit_review(wrong.id, "B")
        assert result["graded"] is True
        assert result["updated"]["last_status"] == "reviewing"
        assert wrong.review_stage == expected_stage
        assert wrong.last_review_at == current_time[0]
        assert wrong.next_review_at.date() == (wrong.last_review_at + timedelta(days=expected_interval)).date()
        current_time[0] = wrong.next_review_at

    result = WrongQuestionService(db_session).submit_review(wrong.id, "B")
    assert result["updated"]["last_status"] == "correct"
    assert wrong.mastery_source == "schedule"
    assert wrong.next_review_at is None
    assert wrong not in WrongQuestionService(db_session).repo.get_due(now=current_time[0])

    WrongQuestionService(db_session).submit_review(wrong.id, "A")
    assert wrong.review_stage == 0
    assert wrong.mastery_source is None
    assert wrong.next_review_at.date() == (wrong.last_review_at + timedelta(days=1)).date()


def test_first_wrong_after_manual_bookmark_and_later_wrong_reset_to_tomorrow(db_session, monkeypatch):
    q = make_question(db_session)
    service = WrongQuestionService(db_session)
    service.add_wrong_question(q.id, source="manual")
    assert service.get_due_reviews()["total"] == 0
    monkeypatch.setattr(
        "app.services.misconception_service.MisconceptionService.analyze_wrong_answer",
        lambda *args, **kwargs: None,
    )
    QuizService(db_session).submit_answer(q.id, "A")
    wrong = service.repo.get_by_question_id(q.id)
    assert wrong.next_review_at.date() == (datetime.now() + timedelta(days=1)).date()
    service.submit_review(wrong.id, "B")
    QuizService(db_session).submit_answer(q.id, "A")
    assert wrong.review_stage == 0
    assert wrong.next_review_at.date() == (datetime.now() + timedelta(days=1)).date()


def test_manual_bookmark_due_tomorrow_and_ungraded_review_does_not_advance_schedule(db_session, monkeypatch):
    q = make_question(db_session, choice=False)
    service = WrongQuestionService(db_session)
    manual = service.add_wrong_question(q.id, source="manual")
    wrong = service.repo.get_by_id(manual["id"])
    assert wrong.next_review_at.date() == (datetime.now() + timedelta(days=1)).date()
    assert service.get_due_reviews()["total"] == 0
    initial_due = wrong.next_review_at
    monkeypatch.setattr(quiz_service, "get_llm_service", lambda: None)
    pending = service.submit_review(wrong.id, "答题")
    assert pending["graded"] is False
    assert wrong.next_review_at == initial_due
    assert wrong.review_count == 0

    assessed = service.self_assess_review(wrong.id, pending["attempt_token"], True)
    assert assessed["graded"] is True
    # The attempt was before the first scheduled day; it is recorded without
    # skipping that first one-day review.
    assert wrong.review_stage == 0
    assert wrong.next_review_at == initial_due
    assert service.get_due_reviews()["total"] == 0


def test_manual_mastery_is_reversible_and_does_not_fabricate_answer(db_session, monkeypatch):
    q = make_question(db_session)
    service = WrongQuestionService(db_session)
    added = service.add_wrong_question(q.id, source="manual")
    wid = added["id"]
    assert service.get_due_reviews()["total"] == 0

    marked = service.mark_mastered(wid)
    assert marked["last_status"] == "correct"
    assert marked["mastery_source"] == "manual"
    assert marked["next_review_at"] is None
    assert marked["review_count"] == 0
    assert service.get_due_reviews()["total"] == 0
    assert db_session.query(QuizRecord).count() == 0
    with pytest.raises(ValueError, match="已标记"):
        service.mark_mastered(wid)

    rejoined = service.rejoin_review(wid)
    assert rejoined["last_status"] == "unreviewed"
    assert rejoined["mastery_source"] is None
    assert rejoined["review_stage"] == 0
    assert service.get_due_reviews()["total"] == 0
    assert service.repo.get_by_id(wid).next_review_at.date() == (datetime.now() + timedelta(days=1)).date()
    with pytest.raises(ValueError, match="尚未标记"):
        service.rejoin_review(wid)
    assert service.mark_mastered(987654) == {"error": "Wrong question not found"}
    assert service.rejoin_review(987654) == {"error": "Wrong question not found"}

    service.mark_mastered(wid)
    # An actual wrong answer after graduation cancels the manual override.
    monkeypatch.setattr(
        "app.services.misconception_service.MisconceptionService.analyze_wrong_answer",
        lambda *args, **kwargs: None,
    )
    QuizService(db_session).submit_answer(q.id, "A")
    wrong = service.repo.get_by_id(wid)
    assert wrong.last_status == "wrong"
    assert wrong.mastery_source is None
    assert wrong.next_review_at.date() == (datetime.now() + timedelta(days=1)).date()


def test_early_correct_review_does_not_skip_an_interval(db_session):
    q = make_question(db_session)
    service = WrongQuestionService(db_session)
    wid = service.add_wrong_question(q.id, source="manual")["id"]
    first = service.submit_review(wid, "B")
    assert first["updated"]["last_status"] == "reviewing"
    scheduled_date = service.repo.get_by_id(wid).next_review_at
    assert service.repo.get_by_id(wid).review_stage == 0
    second = service.submit_review(wid, "B")
    assert second["updated"]["last_status"] == "reviewing"
    wrong = service.repo.get_by_id(wid)
    assert wrong.review_count == 2
    assert wrong.review_stage == 0
    assert wrong.next_review_at == scheduled_date
    assert wrong.mastery_source is None


def test_mastered_item_remains_mastered_after_optional_correct_review(db_session):
    q = make_question(db_session)
    service = WrongQuestionService(db_session)
    wid = service.add_wrong_question(q.id, source="manual")["id"]
    service.mark_mastered(wid)
    result = service.submit_review(wid, "B")
    assert result["updated"]["last_status"] == "correct"
    assert service.repo.get_by_id(wid).mastery_source == "manual"
    assert service.get_due_reviews()["total"] == 0


def test_existing_database_backfills_once_including_manual_bookmarks():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE wrong_questions (
                id INTEGER PRIMARY KEY, question_id INTEGER, source VARCHAR(16),
                review_count INTEGER, added_at DATETIME, last_review_at DATETIME,
                last_status VARCHAR(16)
            )
        """))
        conn.execute(text("""
            INSERT INTO wrong_questions VALUES
                (1, 1, 'auto', 0, '2026-09-01 08:00:00', NULL, 'unreviewed'),
                (2, 2, 'manual', 0, '2026-09-01 08:00:00', NULL, 'unreviewed'),
                (3, 3, 'manual', 2, '2026-09-01 08:00:00', '2026-09-08 10:00:00', 'correct'),
                (4, 4, 'manual', 0, '2026-09-01 08:00:00', NULL, 'wrong')
        """))
    sync_agent_knowledge_schema(engine)
    sync_agent_knowledge_schema(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("wrong_questions")}
    assert {"review_stage", "next_review_at"} <= columns
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, next_review_at FROM wrong_questions ORDER BY id")).all()
    assert rows == [
        (1, "2026-09-02 08:00:00"),
        (2, "2026-09-02 08:00:00"),
        (3, "2026-09-09 10:00:00"),
        (4, "2026-09-02 08:00:00"),
    ]


def test_migration_delays_same_day_first_reviews_once_without_resetting_completed():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE wrong_questions (
                id INTEGER PRIMARY KEY, review_count INTEGER, added_at DATETIME,
                last_review_at DATETIME, next_review_at DATETIME, review_stage INTEGER,
                last_status VARCHAR(16)
            )
        """))
        conn.execute(text("""
            INSERT INTO wrong_questions VALUES
                (1, 0, '2026-09-30 10:00:00', NULL, NULL, 0, 'unreviewed'),
                (2, 0, '2026-09-30 10:00:00', NULL, '2026-10-01 10:00:00', 0, 'unreviewed'),
                (3, 2, '2026-09-01 10:00:00', '2026-09-30 10:00:00', '2026-10-07 10:00:00', 2, 'correct'),
                (4, 0, '2026-09-30 10:00:00', NULL, '2026-09-30 10:00:01', 0, 'unreviewed'),
                (5, 0, '2026-09-30 10:00:00', NULL, NULL, 0, 'correct'),
                (6, 1, '2026-09-30 10:00:00', '2026-09-30 10:05:00', '2026-09-30 10:30:00', 0, 'reviewing'),
                (7, 3, '2026-09-01 10:00:00', '2026-09-30 10:00:00', '2026-09-30 11:00:00', 2, 'reviewing'),
                (8, 1, '2026-09-30 10:00:00', '2026-09-30 10:05:00', '2026-10-01 10:05:00', 0, 'reviewing'),
                (9, 2, '2026-09-01 10:00:00', '2026-09-30 10:00:00', '2026-09-30 11:00:00', 0, 'wrong')
        """))
    sync_agent_knowledge_schema(engine)
    sync_agent_knowledge_schema(engine)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, next_review_at FROM wrong_questions ORDER BY id")).all()
        migrated = conn.execute(text("SELECT last_status, mastery_source, review_stage FROM wrong_questions WHERE id = 3")).one()
    assert rows == [
        (1, "2026-10-01 10:00:00"),
        (2, "2026-10-01 10:00:00"),
        (3, "2026-10-07 10:00:00"),
        (4, "2026-10-01 10:00:00"),
        (5, None),
        (6, "2026-10-01 10:30:00"),
        (7, "2026-09-30 11:00:00"),
        (8, "2026-10-01 10:05:00"),
        (9, "2026-10-01 11:00:00"),
    ]
    assert migrated == ("reviewing", None, 2)