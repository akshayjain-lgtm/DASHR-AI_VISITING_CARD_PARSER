"""wallet_transaction_quantity

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wallet_transactions",
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("wallet_transactions", "quantity")
