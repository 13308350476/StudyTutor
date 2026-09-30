"""Review grading must match normal quiz submissions."""

from app.models.answer_candidate import AnswerCandidate
from app.models.question import Question
from app.models.quiz_record import QuizRecord
from app.models.wrong_question import WrongQuestion
from app.services import quiz_service
from app.services.quiz_service import QuizService
from app.services.wrong_question_service import WrongQuestionService


def test_manual_add_can_collect_unanswered_question(db_session):
    question = Question(
        subject="数据结构", question_type="other", question_text="未作答的综合题",
        answer="",
    )
    db_session.add(question)
    db_session.commit()

    added = WrongQuestionService(db_session).add_wrong_question(question.id, source="manual")
    assert added["question_id"] == question.id
    assert added["last_status"] == "unreviewed"
    assert added["source"] == "manual"
    assert added["next_review_at"] is not None
    assert db_session.query(QuizRecord).count() == 0


def test_repeated_manual_add_preserves_review_status(db_session):
    question, wrong = make_wrong_question(db_session, answer="B", status="correct")
    wrong.review_count = 2
    from datetime import datetime, timedelta
    wrong.next_review_at = datetime.now() + timedelta(days=7)
    db_session.commit()

    service = WrongQuestionService(db_session)
    result = service.add_wrong_question(question.id, source="manual")
    assert result["id"] == wrong.id
    assert result["last_status"] == "correct"
    assert result["review_count"] == 2
    assert result["next_review_at"] == wrong.next_review_at.isoformat()
    assert db_session.query(WrongQuestion).filter_by(question_id=question.id).count() == 1

    service.auto_add(question)  # A real wrong answer must still mark it for review.
    db_session.commit()
    assert service.repo.get_by_question_id(question.id).last_status == "wrong"


def make_wrong_question(db, *, choice=True, answer="", status="unreviewed"):
    question = Question(
        subject="数据结构", chapter="测试章节", knowledge_tag="测试知识点",
        question_type="choice" if choice else "other",
        question_text="树的测试题" if choice else "解释树的遍历",
        option_a="选项A" if choice else None,
        option_b="选项B" if choice else None,
        option_c="选项C" if choice else None,
        option_d="选项D" if choice else None,
        answer=answer,
    )
    db.add(question)
    db.flush()
    wrong = WrongQuestion(
        question_id=question.id, subject=question.subject,
        chapter=question.chapter, last_status=status, review_count=0,
    )
    db.add(wrong)
    db.commit()
    return question, wrong


def test_essay_reference_does_not_mark_wrong_or_change_statistics(db_session, monkeypatch):
    question, wrong = make_wrong_question(
        db_session, choice=False, answer="遍历包含前序、中序与后序", status="correct"
    )
    monkeypatch.setattr(quiz_service, "get_llm_service", lambda: None)
    result = WrongQuestionService(db_session).submit_review(wrong.id, "遍历有几种次序")
    assert result["graded"] is False
    assert result["correct_answer"] == question.answer
    assert result["updated"] == {"last_status": "correct", "review_count": 0}
    assert db_session.query(QuizRecord).count() == 0
    assert QuizService(db_session).get_stats().weak_knowledge == []


def test_essay_without_answer_remains_ungraded(db_session, monkeypatch):
    _, wrong = make_wrong_question(db_session, choice=False, status="unreviewed")

    class NoLLM:
        def is_configured(self):
            return False

    monkeypatch.setattr(quiz_service, "get_llm_service", lambda: NoLLM())
    result = WrongQuestionService(db_session).submit_review(wrong.id, "我的理解")
    assert result["graded"] is False
    assert result["correct_answer"] == "(暂无答案)"
    assert result["updated"] == {"last_status": "unreviewed", "review_count": 0}
    assert db_session.query(QuizRecord).count() == 0


def test_unanswered_choice_does_not_count_as_wrong(db_session, monkeypatch):
    _, wrong = make_wrong_question(db_session, status="correct")

    class NoLLM:
        def is_configured(self):
            return False

    monkeypatch.setattr(quiz_service, "get_llm_service", lambda: NoLLM())
    result = WrongQuestionService(db_session).submit_review(wrong.id, "A")
    assert result["graded"] is False
    assert result["updated"] == {"last_status": "correct", "review_count": 0}
    assert db_session.query(QuizRecord).count() == 0
    assert QuizService(db_session).get_stats().weak_knowledge == []


def test_verified_candidate_is_graded_once(db_session):
    question, wrong = make_wrong_question(db_session)
    db_session.add(AnswerCandidate(
        question_id=question.id, source="deepseek", answer_text="B",
        confidence=0.95, is_verified=True,
    ))
    db_session.commit()
    result = WrongQuestionService(db_session).submit_review(wrong.id, "B")
    assert result["graded"] is True
    assert result["is_correct"] is True
    assert result["answer_source"] == "deepseek"
    assert result["updated"] == {"last_status": "reviewing", "review_count": 1}
    assert db_session.query(QuizRecord).count() == 1
    assert QuizService(db_session).get_stats().total_correct == 1


def test_graded_wrong_then_correct_updates_review_without_duplicate_records(db_session, monkeypatch):
    question, wrong = make_wrong_question(db_session, answer="B")
    # The optional AI misconception analyzer must not affect grading.
    monkeypatch.setattr(
        "app.services.misconception_service.MisconceptionService.analyze_wrong_answer",
        lambda *args, **kwargs: None,
    )
    service = WrongQuestionService(db_session)
    first = service.submit_review(wrong.id, "A")
    assert first["graded"] is True
    assert first["is_correct"] is False
    assert first["updated"] == {"last_status": "wrong", "review_count": 1}
    second = service.submit_review(wrong.id, "B")
    assert second["graded"] is True
    assert second["is_correct"] is True
    assert second["updated"] == {"last_status": "reviewing", "review_count": 2}
    assert db_session.query(WrongQuestion).filter_by(question_id=question.id).count() == 1
    assert db_session.query(QuizRecord).count() == 2
    stats = QuizService(db_session).get_stats()
    assert (stats.total_attempts, stats.total_correct) == (2, 1)
    assert stats.weak_knowledge[0]["wrong_count"] == 1