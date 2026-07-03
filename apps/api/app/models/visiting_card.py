import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VisitingCard(Base):
    __tablename__ = "visiting_cards"

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
    lead_score: Mapped[Decimal | None] = mapped_column(Numeric)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    scored_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(server_default=text("'new'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
