"""wallet_billing

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12

"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_rates",
        sa.Column(
            "pricing_rate_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("rate_inr", sa.Numeric(), nullable=False),
        sa.Column(
            "effective_from", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("pricing_rate_id", name="pk_pricing_rates"),
    )
    op.create_index(
        "ix_pricing_rates_action_type_effective_from",
        "pricing_rates",
        ["action_type", "effective_from"],
    )

    op.create_table(
        "wallets",
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_inr", sa.Numeric(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("wallet_id", name="pk_wallets"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_wallets_user_id_users"),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
    )

    op.create_table(
        "wallet_transactions",
        sa.Column(
            "wallet_transaction_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_type", sa.String(), nullable=False),
        sa.Column("amount_inr", sa.Numeric(), nullable=False),
        sa.Column("balance_after_inr", sa.Numeric(), nullable=False),
        sa.Column("razorpay_order_id", sa.String(), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(), nullable=True),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("wallet_transaction_id", name="pk_wallet_transactions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name="fk_wallet_transactions_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallets.wallet_id"], name="fk_wallet_transactions_wallet_id_wallets"
        ),
    )
    op.create_index(
        "ix_wallet_transactions_user_id_created_at",
        "wallet_transactions",
        ["user_id", "created_at"],
    )
    op.create_index(
        "uq_wallet_transactions_razorpay_order_id",
        "wallet_transactions",
        ["razorpay_order_id"],
        unique=True,
        postgresql_where=sa.text("razorpay_order_id IS NOT NULL"),
    )

    # Seed launch pricing (CLAUDE.md: parse=5, enrichment=3, scoring=2, INR).
    # bulk_insert doesn't apply server defaults, so effective_from/created_at
    # are set explicitly here.
    now = datetime.now(timezone.utc)
    pricing_rates_table = sa.table(
        "pricing_rates",
        sa.column("action_type", sa.String()),
        sa.column("rate_inr", sa.Numeric()),
        sa.column("effective_from", sa.TIMESTAMP(timezone=True)),
        sa.column("created_at", sa.TIMESTAMP(timezone=True)),
    )
    op.bulk_insert(
        pricing_rates_table,
        [
            {"action_type": "parse", "rate_inr": 5, "effective_from": now, "created_at": now},
            {"action_type": "enrichment", "rate_inr": 3, "effective_from": now, "created_at": now},
            {"action_type": "scoring", "rate_inr": 2, "effective_from": now, "created_at": now},
        ],
    )


def downgrade() -> None:
    op.drop_index("uq_wallet_transactions_razorpay_order_id", table_name="wallet_transactions")
    op.drop_index("ix_wallet_transactions_user_id_created_at", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_table("wallets")
    op.drop_index("ix_pricing_rates_action_type_effective_from", table_name="pricing_rates")
    op.drop_table("pricing_rates")
