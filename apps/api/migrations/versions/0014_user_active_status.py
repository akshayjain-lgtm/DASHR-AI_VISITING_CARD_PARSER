"""users.is_active

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "is_active")
