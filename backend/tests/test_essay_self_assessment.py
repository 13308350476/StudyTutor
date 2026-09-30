"""Self-assessment requires a real submission and is recorded only once."""

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database.schema_sync import sync_agent_knowledge_schema
from app.models.question import Question
from app.models.quiz_record import QuizRecord
from app.models.wrong_question import WrongQuestion
from app.services.quiz_service import QuizService
from app.services.wrong_question_service import WrongQuestionService


def essay(db):
    question = Question(
        subject="数据结构", question_type="other", question_text="解释二叉树",
        knowledge_tag="二叉树", answer="一种树形结构",
    )
    db.add(question)
    db.commit()
    return question


@pytest.mark.parametrize("correct", [True, False])
def test_self_assessment_is_once_only_and_separate_from_objective_stats(db_session, correct):
    question = essay(db_session)
    service = QuizService(db_session)
    submitted = service.submit_answer(question.id, "自己的回答" * 40)
    assert submitted.graded is False
    assert submitted.attempt_token
    assert db_session.query(QuizRecord).count() == 0

    result = service.self_assess(submitted.attempt_token, correct)
    assert result.is_correct is correct
    assert result.assessment_source == "self_assessed"
    with pytest.raises(ValueError, match="已自评"):
        service.self_assess(submitted.attempt_token, not correct)
    with pytest.raises(ValueError, match="未找到"):
        service.self_assess("invalid-token", correct)

    records = db_session.query(QuizRecord).all()
    assert len(records) == 1
    assert records[0].user_answer == "自己的回答" * 40
    assert records[0].assessment_source == "self_assessed"
    stats = service.get_stats()
    assert stats.total_attempts == 0
    assert stats.assessment_stats["self_assessed"] == {"total": 1, "correct": int(correct)}
    assert stats.weak_knowledge == []
    assert db_session.query(WrongQuestion).count() == (0 if correct else 1)


def test_review_self_assessment_updates_only_its_own_wrong_question(db_session):
    question = essay(db_session)
    wrong = WrongQuestion(question_id=question.id, subject=question.subject,
                          last_status="wrong", review_count=1)
    db_session.add(wrong)
    db_session.commit()
    service = WrongQuestionService(db_session)
    pending = service.submit_review(wrong.id, "重做的回答")
    assert pending["updated"] == {"last_status": "wrong", "review_count": 1}
    token = pending["attempt_token"]
    with pytest.raises(ValueError, match="未找到"):
        QuizService(db_session).self_assess(token, True)

    result = service.self_assess_review(wrong.id, token, True)
    assert result["updated"] == {"last_status": "reviewing", "review_count": 2}
    with pytest.raises(ValueError, match="已自评"):
        service.self_assess_review(wrong.id, token, False)
    assert db_session.query(QuizRecord).count() == 1


def test_existing_records_get_objective_source_on_schema_sync():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE quiz_records (id INTEGER PRIMARY KEY, question_id INTEGER, user_answer VARCHAR(50), is_correct BOOLEAN, create_time DATETIME)"))
        conn.execute(text("INSERT INTO quiz_records (id, question_id, user_answer, is_correct) VALUES (1, 2, 'A', 1)"))
    sync_agent_knowledge_schema(engine)
    sync_agent_knowledge_schema(engine)
    assert "assessment_source" in {col["name"] for col in inspect(engine).get_columns("quiz_records")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT assessment_source FROM quiz_records WHERE id = 1")).scalar() == "auto"