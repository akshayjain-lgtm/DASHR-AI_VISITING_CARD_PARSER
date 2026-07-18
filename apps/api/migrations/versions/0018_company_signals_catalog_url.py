"""company_signals.catalog_url

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_signals", sa.Column("catalog_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("company_signals", "catalog_url")
