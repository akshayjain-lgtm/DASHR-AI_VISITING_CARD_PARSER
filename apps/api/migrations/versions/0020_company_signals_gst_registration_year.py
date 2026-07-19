"""company_signals.indiamart_gst_registration_date -> indiamart_gst_registration_year

Confirmed live against a real supplier that the actor's gstRegistrationDate
field only ever carries a bare year (e.g. "2017"), never a full date — this
replaces the Date column (shipped as an always-null placeholder before this
was confirmed) with an Integer year column, rather than fabricating a Jan-1
calendar date out of information we don't actually have.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-19

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("company_signals", "indiamart_gst_registration_date")
    op.add_column("company_signals", sa.Column("indiamart_gst_registration_year", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("company_signals", "indiamart_gst_registration_year")
    op.add_column("company_signals", sa.Column("indiamart_gst_registration_date", sa.Date(), nullable=True))
