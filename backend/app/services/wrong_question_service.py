"""WrongQuestion Service — business logic for wrong question management."""

from sqlalchemy.orm import Session

from app.core.exceptions import QuestionNotFoundError
from app.core.logging_config import get_logger
from app.models.question import Question
from app.repositories.question_repo import QuestionRepository
from app.repositories.wrong_question_repo import WrongQuestionRepository
from app.services.quiz_service import QuizService

logger = get_logger("wrong_question_service")


class WrongQuestionService:
    """Service for wrong question collection, management, and re-challenge."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = WrongQuestionRepository(db)
        self.question_repo = QuestionRepository(db)

    # ── Add ──

    def add_wrong_question(
        self, question_id: int, source: str = "auto"
    ) -> dict:
        """Add a question to the wrong question collection.

        A duplicate manual add preserves the existing review status.
        """
        question = self.question_repo.get_by_id(question_id)
        if not question:
            raise QuestionNotFoundError(f"Question not found: id={question_id}")

        wq = self.repo.add_or_update(
            question_id=question_id,
            subject=question.subject,
            chapter=question.chapter,
            source=source,
        )
        self.db.commit()
        logger.info(f"Wrong question added: qid={question_id}, source={source}")
        return self._serialize(wq)

    def auto_add(self, question: Question) -> None:
        """Auto-add during quiz grading (no commit — caller commits)."""
        self.repo.add_or_update(
            question_id=question.id,
            subject=question.subject,
            chapter=question.chapter,
            source="auto",
        )

    # ── Remove ──

    def remove_wrong_question(self, wrong_id: int) -> bool:
        """Remove a wrong question from the collection."""
        ok = self.repo.remove(wrong_id)
        if ok:
            self.db.commit()
            logger.info(f"Wrong question removed: id={wrong_id}")
        return ok

    def batch_remove(self, wrong_ids: list[int]) -> int:
        """Remove multiple wrong questions."""
        count = self.repo.batch_remove(wrong_ids)
        self.db.commit()
        logger.info(f"Batch removed {count} wrong questions")
        return count

    # ── List ──

    def list_wrong_questions(
        self,
        subject: str | None = None,
        chapter: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Paginated, filtered list."""
        offset = (page - 1) * page_size
        items = self.repo.list_filtered(subject, chapter, status, offset, page_size)
        total = self.repo.count_filtered(subject, chapter, status)
        pages = (total + page_size - 1) // page_size if total > 0 else 1

        return {
            "items": [self._serialize_with_preview(wq) for wq in items],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    # ── Detail ──

    def get_detail(self, wrong_id: int) -> dict:
        """Full detail including question text and options."""
        wq = self.repo.get_with_question(wrong_id)
        if not wq:
            return {}
        return self._serialize_detail(wq)

    # ── Re-challenge ──

    def get_batch_for_review(self, wrong_ids: list[int]) -> list[dict]:
        """Get question data for a batch re-challenge session (answers hidden)."""
        results = []
        for wid in wrong_ids:
            wq = self.repo.get_with_question(wid)
            if wq and wq.question:
                q = wq.question
                results.append({
                    "wrong_id": wq.id,
                    "question_id": q.id,
                    "subject": q.subject,
                    "chapter": q.chapter,
                    "question_text": q.question_text,
                    "question_type": q.question_type,
                    "option_a": q.option_a,
                    "option_b": q.option_b,
                    "option_c": q.option_c,
                    "option_d": q.option_d,
                    "image_path": q.image_path,
                })
        return results

    def submit_review(self, wrong_id: int, user_answer: str) -> dict:
        """Use the same grading as practice; update review status only when graded."""
        wq = self.repo.get_with_question(wrong_id)
        if not wq or not wq.question:
            return {"error": "Wrong question not found"}

        result = QuizService(self.db).submit_answer(wq.question_id, user_answer)
        if result.graded:
            self.repo.update_review(wrong_id, result.is_correct)
            self.db.commit()

        logger.info(
            f"Re-challenge: wq#{wrong_id} Q#{wq.question_id}, "
            f"graded={result.graded}, match={result.is_correct if result.graded else 'n/a'}"
        )

        return {
            **result.model_dump(),
            "updated": {
                "last_status": wq.last_status,
                "review_count": wq.review_count,
            },
        }

    # ── Stats ──

    def get_stats(self) -> dict:
        """Aggregate statistics."""
        return self.repo.get_stats()

    def get_chapters(self, subject: str) -> list[str]:
        """Chapters for a given subject."""
        return self.repo.get_chapters_for_subject(subject)

    # ── Serialization helpers ──

    @staticmethod
    def _serialize(wq) -> dict:
        return {
            "id": wq.id,
            "question_id": wq.question_id,
            "source": wq.source,
            "subject": wq.subject,
            "chapter": wq.chapter or "",
            "last_status": wq.last_status,
            "review_count": wq.review_count,
            "added_at": wq.added_at.isoformat() if wq.added_at else None,
            "last_review_at": wq.last_review_at.isoformat() if wq.last_review_at else None,
        }

    @staticmethod
    def _serialize_with_preview(wq) -> dict:
        """List item with truncated question text preview."""
        result = WrongQuestionService._serialize(wq)
        q = wq.question
        if q:
            text = q.question_text or ""
            result["question_text_preview"] = text[:80] + ("..." if len(text) > 80 else "")
            result["question_type"] = q.question_type
        else:
            result["question_text_preview"] = ""
            result["question_type"] = ""
        return result

    @staticmethod
    def _serialize_detail(wq) -> dict:
        """Full detail with complete question data."""
        result = WrongQuestionService._serialize(wq)
        q = wq.question
        if q:
            result["question"] = {
                "id": q.id,
                "subject": q.subject,
                "chapter": q.chapter,
                "question_text": q.question_text,
                "question_type": q.question_type,
                "option_a": q.option_a,
                "option_b": q.option_b,
                "option_c": q.option_c,
                "option_d": q.option_d,
                "answer": q.answer,
                "analysis": q.analysis,
                "image_path": q.image_path,
                "knowledge_tag": q.knowledge_tag,
            }
        return result
