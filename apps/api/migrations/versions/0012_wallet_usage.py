"""wallet_usage

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pricing_rates",
        sa.Column("free_limit", sa.Integer(), server_default="20", nullable=False),
    )

    op.create_table(
        "free_action_allowances",
        sa.Column(
            "free_action_allowance_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("free_action_allowance_id", name="pk_free_action_allowances"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name="fk_free_action_allowances_user_id_users"
        ),
    )
    op.create_index(
        "uq_free_action_allowances_user_id_action_type",
        "free_action_allowances",
        ["user_id", "action_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_free_action_allowances_user_id_action_type", table_name="free_action_allowances"
    )
    op.drop_table("free_action_allowances")
    op.drop_column("pricing_rates", "free_limit")
