import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SupportQuery(Base):
    """A "raise a query" submission from the Feedback page — see
    .claude/specs/23-feedback-and-support.md. `ticket_id` is minted from
    `support_query_ticket_seq` (services/feedback_service.py), never a
    client-supplied value. `email_sent` is set only after
    SupportQueryEmailProvider.send() returns without raising, so a later
    ops step can find queries whose notification failed without re-deriving
    that from logs."""

    __tablename__ = "support_queries"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_support_queries_status_valid"),
        Index("ix_support_queries_user_id_created_at", "user_id", "created_at"),
    )

    support_query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="SET NULL")
    )
    ticket_id: Mapped[str] = mapped_column(unique=True)
    subject: Mapped[str]
    message: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    email_sent: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
