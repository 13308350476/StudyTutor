"""Manual standard-answer entry and future grading."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.deps import get_db
from app.database.base import Base
from app.main import app
from app.models.answer_candidate import AnswerCandidate
from app.models.question import Question
from app.services.quiz_service import QuizService


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def unanswered_question(db_session):
    question = Question(
        subject="数据结构",
        question_type="choice",
        question_text="测试未附答案题",
        option_a="选项一",
        option_b="选项二",
        option_c="选项三",
        option_d="选项四",
        answer="",
        text_hash="manual_answer_test",
    )
    db_session.add(question)
    db_session.commit()
    return question


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_manual_answer_persists_and_grades_future_attempt(
    client, db_session, unanswered_question, monkeypatch
):
    from app.services import quiz_service

    class UnconfiguredLLM:
        def is_configured(self):
            return False

    monkeypatch.setattr(quiz_service, "get_llm_service", lambda: UnconfiguredLLM())
    service = QuizService(db_session)
    first = service.submit_answer(unanswered_question.id, "A")
    assert first.graded is False
    assert service.get_stats().total_attempts == 0

    response = client.put(
        f"/api/questions/{unanswered_question.id}/answer",
        json={"answer": "   ", "analysis": "核对过原书"},
    )
    assert response.status_code == 400  # whitespace-only answers are rejected
    response = client.put(
        f"/api/questions/{unanswered_question.id}/answer",
        json={"answer": "b", "analysis": "核对过原书"},
    )
    assert response.status_code == 200
    assert response.json()["answer_source"] == "user_confirmed"
    db_session.expire_all()
    assert db_session.get(Question, unanswered_question.id).answer == "B"
    assert db_session.get(Question, unanswered_question.id).analysis == "核对过原书"
    candidate = db_session.query(AnswerCandidate).filter_by(
        question_id=unanswered_question.id
    ).one()
    assert (candidate.source, candidate.answer_text, candidate.is_verified) == (
        "user_confirmed", "B", True
    )
    assert service.get_stats().total_attempts == 0

    second = client.post(
        "/api/quiz/submit",
        json={"question_id": unanswered_question.id, "user_answer": "A"},
    )
    assert second.status_code == 200
    assert second.json()["graded"] is True
    assert second.json()["correct_answer"] == "B"
    assert second.json()["answer_source"] == "user_confirmed"
    assert second.json()["is_correct"] is False
    assert service.get_stats().total_attempts == 1


def test_manual_answer_rejects_invalid_or_existing(
    client, db_session, unanswered_question
):
    url = f"/api/questions/{unanswered_question.id}/answer"
    for answer in ("Z", "AB", ""):
        response = client.put(url, json={"answer": answer})
        assert response.status_code in (400, 422)
    unanswered_question.option_d = None
    db_session.commit()
    assert client.put(url, json={"answer": "D"}).status_code == 400
    assert db_session.get(Question, unanswered_question.id).answer == ""
    assert db_session.query(AnswerCandidate).count() == 0

    assert client.put(url, json={"answer": "A"}).status_code == 200
    assert client.put(url, json={"answer": "B"}).status_code == 409
    assert db_session.get(Question, unanswered_question.id).answer == "A"
    assert db_session.query(AnswerCandidate).count() == 1
    assert client.put("/api/questions/999999/answer", json={"answer": "A"}).status_code == 404


def test_correct_existing_answer_requires_confirmation(client, db_session, unanswered_question):
    unanswered_question.answer = "A"
    unanswered_question.analysis = "原解析"
    db_session.commit()
    service = QuizService(db_session)
    previous = service.submit_answer(unanswered_question.id, "A")
    assert previous.graded is True
    assert service.get_stats().total_correct == 1

    url = f"/api/questions/{unanswered_question.id}/answer"
    assert client.put(url, json={"answer": "B"}).status_code == 409
    assert db_session.get(Question, unanswered_question.id).answer == "A"
    response = client.put(url, json={"answer": "B", "confirm_overwrite": True})
    assert response.status_code == 200
    assert response.json()["answer"] == "B"
    assert response.json()["analysis"] == "原解析"  # blank input preserves it
    assert service.get_stats().total_correct == 1  # history is not recalculated
    assert service.submit_answer(unanswered_question.id, "B").is_correct is True
    assert service.get_stats().total_attempts == 2
    assert service.get_stats().total_correct == 2
    candidate = db_session.query(AnswerCandidate).filter_by(
        question_id=unanswered_question.id
    ).one()
    assert candidate.source == "user_confirmed"


def test_manual_long_form_reference_is_saved_but_not_graded(client, db_session, monkeypatch):
    question = Question(
        subject="操作系统", question_type="other", question_text="论述题",
        answer="",
    )
    db_session.add(question)
    db_session.commit()
    reference = "参考步骤：" + "说明原理并分析细节。" * 15
    response = client.put(
        f"/api/questions/{question.id}/answer",
        json={"answer": reference},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == reference
    db_session.expire_all()
    assert db_session.get(Question, question.id).answer == reference
    assert db_session.query(AnswerCandidate).filter_by(question_id=question.id).one().is_verified

    from app.services import quiz_service

    def should_not_call_ai():
        raise AssertionError("manual reference should not trigger AI generation")

    monkeypatch.setattr(quiz_service, "get_llm_service", should_not_call_ai)
    result = QuizService(db_session).submit_answer(question.id, "我的答案")
    assert result.correct_answer == reference
    assert result.analysis is None
    assert result.graded is False
    assert result.answer_source == "user_confirmed"
    assert QuizService(db_session).get_stats().total_attempts == 0

    updated = client.put(
        f"/api/questions/{question.id}/answer",
        json={"answer": reference, "analysis": "逐步分析", "confirm_overwrite": True},
    )
    assert updated.status_code == 200
    assert QuizService(db_session).submit_answer(question.id, "我的答案").analysis == "逐步分析"


def test_reference_requires_nonblank_and_overwrite_confirmation(client, db_session):
    question = Question(subject="操作系统", question_type="other", question_text="论述题", answer="原答案")
    db_session.add(question)
    db_session.commit()
    url = f"/api/questions/{question.id}/answer"
    assert client.put(url, json={"answer": "   ", "confirm_overwrite": True}).status_code == 400
    assert client.put(url, json={"answer": "新答案"}).status_code == 409
    assert client.put(url, json={"answer": "新答案", "confirm_overwrite": True}).status_code == 200
    assert db_session.get(Question, question.id).answer == "新答案"