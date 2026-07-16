import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Numeric, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PricingRate(Base):
    """Configurable per-action pricing (CLAUDE.md: prices are data, never
    hardcoded). Global reference data, not org/user-scoped — parse/enrichment/
    scoring rates apply platform-wide at launch. Versioned via effective_from
    so historical invoices stay correct if rates change later."""

    __tablename__ = "pricing_rates"
    __table_args__ = (
        Index("ix_pricing_rates_action_type_effective_from", "action_type", "effective_from"),
    )

    pricing_rate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # "parse" | "enrichment" | "scoring"
    action_type: Mapped[str]
    rate_inr: Mapped[Decimal] = mapped_column(Numeric)
    # Free-action cap for this action type, versioned alongside rate_inr on
    # the same row — a free-limit change becomes a new row via effective_from,
    # same as a rate change.
    free_limit: Mapped[int] = mapped_column(server_default=text("20"))
    effective_from: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
