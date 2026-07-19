import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FieldCorrection(Base):
    """Append-only audit row for every user-made correction to an
    AI-extracted or enriched field — never updated or deleted, mirrors
    WalletTransaction/CompanyEnrichment's ledger/audit convention."""

    __tablename__ = "field_corrections"
    __table_args__ = (
        Index("ix_field_corrections_org_id", "org_id"),
        Index("ix_field_corrections_card_id", "card_id"),
        Index("ix_field_corrections_field_name", "field_name"),
    )

    correction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Nullable only because User.org_id itself is nullable (mirrors that
    # column) — not a deliberate org_id-everywhere exemption.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="SET NULL")
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("visiting_cards.card_id", ondelete="CASCADE"), nullable=False
    )
    corrected_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(nullable=False)
    # CardEmail.email_id / CardPhone.phone_id when field_name is "email"/"phone"; else null.
    record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    original_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
