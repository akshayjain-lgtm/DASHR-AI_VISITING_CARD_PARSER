import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FreeActionAllowance(Base):
    """Per-User, per-action-type lifetime usage counter (CLAUDE.md) that gates
    when parse/enrichment/scoring debits begin. used_count increments on every
    action of that type, whether free or wallet-billed — never reset or
    decremented, and never shared across action types: parse/enrichment/
    scoring each get their own independent (user_id, action_type) row."""

    __tablename__ = "free_action_allowances"
    __table_args__ = (
        Index(
            "uq_free_action_allowances_user_id_action_type",
            "user_id",
            "action_type",
            unique=True,
        ),
    )

    free_action_allowance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    # "parse" | "enrichment" | "scoring"
    action_type: Mapped[str]
    used_count: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
