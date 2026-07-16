import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WalletTransaction(Base):
    """Append-only wallet ledger entry — never updated or deleted, only
    inserted (CLAUDE.md). balance_after_inr snapshots wallets.balance_inr
    immediately after this entry so the ledger is independently auditable/
    reconstructable, not just a side effect of the cached balance column.

    razorpay_order_id is unique (where set) so a redelivered Razorpay webhook
    for an already-credited order can never double-credit the wallet."""

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        Index("ix_wallet_transactions_user_id_created_at", "user_id", "created_at"),
        Index(
            "uq_wallet_transactions_razorpay_order_id",
            "razorpay_order_id",
            unique=True,
            postgresql_where=text("razorpay_order_id IS NOT NULL"),
        ),
    )

    wallet_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Denormalized alongside wallet_id so the ledger-history query never
    # needs a join to wallets just to scope by owner.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.wallet_id"), nullable=False
    )
    # "recharge_credit" | "parse_debit" | "enrichment_debit" | "scoring_debit" | "adjustment"
    transaction_type: Mapped[str]
    # Positive for credits, negative for debits.
    amount_inr: Mapped[Decimal] = mapped_column(Numeric)
    balance_after_inr: Mapped[Decimal] = mapped_column(Numeric)
    razorpay_order_id: Mapped[str | None]
    razorpay_payment_id: Mapped[str | None]
    # e.g. card_id for a single-action debit row (quantity=1); always NULL
    # for a collective bulk-action row (quantity>1), since there's no one
    # card to point at — see quantity below.
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # How many parse/enrich/score actions this single row covers. 1 for a
    # single-card action or a recharge/adjustment; >1 for a bulk parse/
    # enrich/score batch, which is billed as one collective ledger row
    # (amount_inr = rate * quantity) rather than one row per card, so the
    # transaction history stays readable for a large batch.
    quantity: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
