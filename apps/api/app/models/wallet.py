import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Wallet(Base):
    """One prepaid INR balance per User, never per Organization (CLAUDE.md:
    wallets are individually owned — no shared org balance, no admin spending
    authority over a sub-user's wallet). balance_inr is a cached/derived
    value; every change to it is preceded by a WalletTransaction ledger
    insert in services/billing.py — never written to directly elsewhere."""

    __tablename__ = "wallets"

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False, unique=True
    )
    balance_inr: Mapped[Decimal] = mapped_column(Numeric, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
