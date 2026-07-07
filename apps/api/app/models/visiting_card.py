import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VisitingCard(Base):
    __tablename__ = "visiting_cards"
    __table_args__ = (
        Index("ix_visiting_cards_user_id_status", "user_id", "status"),
        Index("ix_visiting_cards_upload_batch_id", "upload_batch_id"),
    )

    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.company_id")
    )
    exhibition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exhibitions.exhibition_id")
    )
    full_name: Mapped[str | None]
    job_title: Mapped[str | None]
    designation_level: Mapped[str | None]
    raw_ocr_text: Mapped[str | None]
    image_url: Mapped[str | None]
    special_remark: Mapped[str | None]
    original_filename: Mapped[str | None]
    website: Mapped[str | None]
    address: Mapped[str | None]
    products_offered: Mapped[str | None]
    upload_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    batch_sequence: Mapped[int | None]
    merged_into_card_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("visiting_cards.card_id")
    )
    extraction_error: Mapped[str | None]
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    lead_score: Mapped[Decimal | None] = mapped_column(Numeric)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    scored_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(server_default=text("'new'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
