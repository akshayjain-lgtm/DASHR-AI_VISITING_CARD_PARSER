"""seller_profiles.designation

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("seller_profiles", sa.Column("designation", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("seller_profiles", "designation")
