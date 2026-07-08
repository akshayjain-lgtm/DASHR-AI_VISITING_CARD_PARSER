"""visiting_cards.gst_number

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-07

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visiting_cards", sa.Column("gst_number", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("visiting_cards", "gst_number")
