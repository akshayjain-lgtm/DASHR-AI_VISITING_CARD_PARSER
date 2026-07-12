"""seller_profiles.gst_no_billing_address

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("seller_profiles", sa.Column("gst_no", sa.String(), nullable=True))
    op.add_column("seller_profiles", sa.Column("billing_address", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("seller_profiles", "billing_address")
    op.drop_column("seller_profiles", "gst_no")
