import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Feedback(Base):
    """Open-ended "what's working" / "what went wrong" submission from the
    Feedback page — see .claude/specs/23-feedback-and-support.md. Stored
    purely for later internal product review; never surfaced back to any
    user, and no email is sent for it (unlike SupportQuery)."""

    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(
            "what_worked IS NOT NULL OR what_went_wrong IS NOT NULL",
            name="ck_feedback_at_least_one_field",
        ),
        Index("ix_feedback_user_id_created_at", "user_id", "created_at"),
    )

    feedback_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    # Nullable to mirror User.org_id itself (a member/admin without an org
    # can still leave feedback) — same pattern as Invoice.org_id.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="SET NULL")
    )
    what_worked: Mapped[str | None]
    what_went_wrong: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
