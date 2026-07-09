"""companies.summary, company_signals

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "companies",
        sa.Column("summary_generated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "company_signals",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cin", sa.String(), nullable=True),
        sa.Column("incorporation_date", sa.Date(), nullable=True),
        sa.Column("registry_status", sa.String(), nullable=True),
        sa.Column("registered_address", sa.Text(), nullable=True),
        sa.Column("authorized_capital", sa.Numeric(), nullable=True),
        sa.Column("paid_up_capital", sa.Numeric(), nullable=True),
        sa.Column("gstin_verified", sa.Boolean(), nullable=True),
        sa.Column("gstin_status", sa.String(), nullable=True),
        sa.Column("udyam_registered", sa.Boolean(), nullable=True),
        sa.Column("udyam_category", sa.String(), nullable=True),
        sa.Column("linkedin_employee_count", sa.Integer(), nullable=True),
        sa.Column("linkedin_follower_count", sa.Integer(), nullable=True),
        sa.Column("estimated_revenue_band", sa.String(), nullable=True),
        sa.Column("product_lines_summary", sa.Text(), nullable=True),
        sa.Column("plant_size_signal", sa.Text(), nullable=True),
        sa.Column("active_job_postings_count", sa.Integer(), nullable=True),
        sa.Column("hiring_signal", sa.String(), nullable=True),
        sa.Column("gem_tender_count", sa.Integer(), nullable=True),
        sa.Column("gem_total_tender_value", sa.Numeric(), nullable=True),
        sa.Column("import_export_activity", sa.Boolean(), nullable=True),
        sa.Column("shipment_count_last_12m", sa.Integer(), nullable=True),
        sa.Column("recent_news_signals", postgresql.JSONB(), nullable=True),
        sa.Column("google_rating", sa.Numeric(), nullable=True),
        sa.Column("google_review_count", sa.Integer(), nullable=True),
        sa.Column("marketplace_vintage_years", sa.Integer(), nullable=True),
        sa.Column("marketplace_verified_badge", sa.Boolean(), nullable=True),
        sa.Column("marketplace_located_in_industrial_area", sa.Boolean(), nullable=True),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("company_id", name="pk_company_signals"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.company_id"],
            name="fk_company_signals_company_id_companies",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("company_signals")
    op.drop_column("companies", "summary_generated_at")
    op.drop_column("companies", "summary")
