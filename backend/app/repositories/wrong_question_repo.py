"""WrongQuestion Repository — data access for wrong question records."""

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.wrong_question import WrongQuestion
from app.repositories.base import BaseRepository


class WrongQuestionRepository(BaseRepository[WrongQuestion]):
    """Data access layer for WrongQuestion model."""

    model = WrongQuestion

    REVIEW_INTERVALS = (1, 3, 7, 14)

    def __init__(self, db: Session):
        super().__init__(db)

    def add_or_update(
        self,
        question_id: int,
        subject: str,
        chapter: str | None,
        source: str = "auto",
    ) -> WrongQuestion:
        """Upsert: insert new or update existing wrong question.

        An automatic wrong answer marks an existing entry as 'wrong'; manually
        re-adding a question leaves its review status untouched. Both sources
        receive a first review one day after collection.
        Returns the WrongQuestion row.
        """
        existing = (
            self.db.query(WrongQuestion)
            .filter(WrongQuestion.question_id == question_id)
            .first()
        )
        if existing:
            if source == "auto":
                existing.last_status = "wrong"
                existing.mastery_source = None
                existing.review_stage = 0
                existing.next_review_at = datetime.now() + timedelta(days=self.REVIEW_INTERVALS[0])
            elif existing.review_count == 0 and existing.next_review_at is None:
                # A legacy unscheduled bookmark can join the plan without
                # changing its existing review status.
                existing.next_review_at = (existing.added_at or datetime.now()) + timedelta(days=self.REVIEW_INTERVALS[0])
            existing.subject = subject
            existing.chapter = chapter
            self.db.flush()
            return existing

        wq = WrongQuestion(
            question_id=question_id,
            source=source,
            subject=subject,
            chapter=chapter or "",
            last_status="unreviewed",
            review_count=0,
            review_stage=0,
            next_review_at=datetime.now() + timedelta(days=self.REVIEW_INTERVALS[0]),
        )
        self.db.add(wq)
        self.db.flush()
        return wq

    def remove(self, wrong_id: int) -> bool:
        """Delete by wrong_questions.id. Returns True if deleted."""
        return self.delete(wrong_id)

    def remove_by_question_id(self, question_id: int) -> bool:
        """Delete by question_id FK."""
        wq = (
            self.db.query(WrongQuestion)
            .filter(WrongQuestion.question_id == question_id)
            .first()
        )
        if wq:
            self.db.delete(wq)
            self.db.flush()
            return True
        return False

    def batch_remove(self, wrong_ids: list[int]) -> int:
        """Delete multiple wrong questions. Returns count deleted."""
        count = 0
        for wid in wrong_ids:
            if self.delete(wid):
                count += 1
        return count

    def get_with_question(self, wrong_id: int) -> WrongQuestion | None:
        """Get single row with eagerly loaded question."""
        return (
            self.db.query(WrongQuestion)
            .options(joinedload(WrongQuestion.question))
            .filter(WrongQuestion.id == wrong_id)
            .first()
        )

    def list_filtered(
        self,
        subject: str | None = None,
        chapter: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[WrongQuestion]:
        """Paginated, filtered list with eager-loaded question."""
        q = self.db.query(WrongQuestion).options(
            joinedload(WrongQuestion.question)
        )
        if subject:
            q = q.filter(WrongQuestion.subject == subject)
        if chapter:
            q = q.filter(WrongQuestion.chapter == chapter)
        if status and status != "all":
            q = q.filter(WrongQuestion.last_status == status)
        return (
            q.order_by(WrongQuestion.added_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def count_filtered(
        self,
        subject: str | None = None,
        chapter: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count matching rows."""
        q = self.db.query(WrongQuestion)
        if subject:
            q = q.filter(WrongQuestion.subject == subject)
        if chapter:
            q = q.filter(WrongQuestion.chapter == chapter)
        if status and status != "all":
            q = q.filter(WrongQuestion.last_status == status)
        return q.count()

    def update_review(self, wrong_id: int, is_correct: bool) -> WrongQuestion | None:
        """Advance only a due, correct review; graduate after the 14-day recall."""
        wq = self.get_by_id(wrong_id)
        if not wq:
            return None
        wq.review_count += 1
        now = datetime.now()
        wq.last_review_at = now
        if not is_correct:
            wq.last_status = "wrong"
            wq.mastery_source = None
            wq.review_stage = 0
            wq.next_review_at = now + timedelta(days=self.REVIEW_INTERVALS[0])
        elif wq.last_status == "correct":
            # An optional practice attempt must not undo a mastered item.
            wq.next_review_at = None
        elif wq.next_review_at and wq.next_review_at.date() > now.date():
            # Early voluntary practice counts as an attempt, not a scheduled
            # review; it must not skip the 3/7/14-day intervals.
            wq.last_status = "reviewing"
        elif (wq.review_stage or 0) >= 3:
            wq.last_status = "correct"
            wq.mastery_source = "schedule"
            wq.next_review_at = None
        else:
            wq.last_status = "reviewing"
            wq.review_stage = (wq.review_stage or 0) + 1
            wq.next_review_at = now + timedelta(days=self.REVIEW_INTERVALS[wq.review_stage])
        self.db.flush()
        return wq

    def mark_mastered(self, wrong_id: int) -> WrongQuestion | None:
        """A manual override does not create a quiz attempt or change review_count."""
        wq = self.get_by_id(wrong_id)
        if wq is None:
            return None
        if wq.last_status == "correct":
            raise ValueError("这道题已标记为已掌握")
        wq.last_status = "correct"
        wq.mastery_source = "manual"
        wq.next_review_at = None
        self.db.flush()
        return wq

    def rejoin_review(self, wrong_id: int) -> WrongQuestion | None:
        """Restart a mastered item's review plan at the one-day first stage."""
        wq = self.get_by_id(wrong_id)
        if wq is None:
            return None
        if wq.last_status != "correct":
            raise ValueError("这道题尚未标记为已掌握")
        wq.last_status = "reviewing" if wq.review_count else "unreviewed"
        wq.mastery_source = None
        wq.review_stage = 0
        wq.next_review_at = datetime.now() + timedelta(days=self.REVIEW_INTERVALS[0])
        self.db.flush()
        return wq

    def get_due(self, now: datetime | None = None) -> list[WrongQuestion]:
        """All overdue and due-today scheduled questions, earliest first."""
        today_end = datetime.combine((now or datetime.now()).date() + timedelta(days=1), datetime.min.time())
        return (
            self.db.query(WrongQuestion)
            .filter(WrongQuestion.next_review_at.isnot(None))
            .filter(WrongQuestion.last_status != "correct")
            .filter(WrongQuestion.next_review_at < today_end)
            .order_by(WrongQuestion.next_review_at.asc(), WrongQuestion.id.asc())
            .all()
        )

    def get_stats(self) -> dict:
        """Aggregate stats: total, by_subject, by_status."""
        total = self.count()

        # By subject
        subject_rows = (
            self.db.query(WrongQuestion.subject, func.count(WrongQuestion.id))
            .group_by(WrongQuestion.subject)
            .all()
        )
        by_subject = {row[0]: row[1] for row in subject_rows}

        # By status
        status_rows = (
            self.db.query(WrongQuestion.last_status, func.count(WrongQuestion.id))
            .group_by(WrongQuestion.last_status)
            .all()
        )
        by_status = {row[0]: row[1] for row in status_rows}

        # Review rate
        reviewed = by_status.get("correct", 0) + by_status.get("wrong", 0) + by_status.get("reviewing", 0)
        review_rate = reviewed / total if total > 0 else 0.0

        return {
            "total": total,
            "by_subject": by_subject,
            "by_status": by_status,
            "review_rate": round(review_rate, 2),
        }

    def get_chapters_for_subject(self, subject: str) -> list[str]:
        """Distinct chapters within a subject."""
        rows = (
            self.db.query(WrongQuestion.chapter)
            .filter(WrongQuestion.subject == subject)
            .filter(WrongQuestion.chapter.isnot(None))
            .filter(WrongQuestion.chapter != "")
            .distinct()
            .order_by(WrongQuestion.chapter)
            .all()
        )
        return [row[0] for row in rows]

    def get_by_question_id(self, question_id: int) -> WrongQuestion | None:
        """Find by question_id FK."""
        return (
            self.db.query(WrongQuestion)
            .filter(WrongQuestion.question_id == question_id)
            .first()
        )
