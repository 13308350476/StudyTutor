"""An ungraded essay submission awaiting one user assessment."""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class EssayAttempt(Base):
    __tablename__ = "essay_attempts"

    token: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    question_id: Mapped[int] = mapped_column(Integer, ForeignKey("questions.id"), nullable=False)
    wrong_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_answer: Mapped[str] = mapped_column(Text, nullable=False)
    assessed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)