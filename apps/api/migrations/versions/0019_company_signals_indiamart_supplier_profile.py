"""company_signals IndiaMART supplier-profile columns

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_signals", sa.Column("indiamart_rating", sa.Numeric(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_rating_count", sa.Integer(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_member_since_year", sa.Integer(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_business_type", sa.String(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_employee_count_band", sa.String(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_annual_turnover_band", sa.String(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_year_established", sa.String(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_gst_number", sa.String(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_gst_registration_date", sa.Date(), nullable=True))
    op.add_column("company_signals", sa.Column("indiamart_call_response_rate", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("company_signals", "indiamart_call_response_rate")
    op.drop_column("company_signals", "indiamart_gst_registration_date")
    op.drop_column("company_signals", "indiamart_gst_number")
    op.drop_column("company_signals", "indiamart_year_established")
    op.drop_column("company_signals", "indiamart_annual_turnover_band")
    op.drop_column("company_signals", "indiamart_employee_count_band")
    op.drop_column("company_signals", "indiamart_business_type")
    op.drop_column("company_signals", "indiamart_member_since_year")
    op.drop_column("company_signals", "indiamart_rating_count")
    op.drop_column("company_signals", "indiamart_rating")
