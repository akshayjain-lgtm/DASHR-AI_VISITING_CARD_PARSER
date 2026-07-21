"""lead scoring v2 product fit + geocoding

Two new cross-org shared caches for the refined lead-scoring v2 categories
(see .claude/specs/10-lead-scoring.md "v2 rework only"): product_fit_judgments
caches a Claude judgment of whether a buyer's industry/business-type would
use a seller's product operationally, geocoded_addresses caches address ->
lat/lon lookups for real aerial-distance proximity scoring. Neither carries
an org_id — the questions they answer ("does industry X need product Y",
"where is this address") have no tenant-specific answer, same rationale as
companies/company_signals.

Also adds four nullable company_signals columns for the extended news
pipeline (AI-summarized articles + share-price QOQ), additive alongside the
existing recent_news_signals column which is unchanged.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-20

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_fit_judgments",
        sa.Column(
            "judgment_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("product_signature_hash", sa.String(), nullable=False),
        sa.Column("buyer_industry_normalized", sa.String(), server_default="", nullable=False),
        sa.Column("buyer_business_type", sa.String(), server_default="", nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("judgment_id", name="pk_product_fit_judgments"),
    )
    op.create_index(
        "ix_product_fit_judgments_lookup",
        "product_fit_judgments",
        ["product_signature_hash", "buyer_industry_normalized", "buyer_business_type"],
    )

    op.create_table(
        "geocoded_addresses",
        sa.Column("address_hash", sa.String(), nullable=False),
        sa.Column("raw_address", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Numeric(), nullable=True),
        sa.Column("longitude", sa.Numeric(), nullable=True),
        sa.Column(
            "geocoded_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("address_hash", name="pk_geocoded_addresses"),
    )

    op.add_column("company_signals", sa.Column("news_summary", sa.Text(), nullable=True))
    op.add_column(
        "company_signals",
        sa.Column("news_summary_generated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "company_signals", sa.Column("news_tags", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "company_signals", sa.Column("share_price_qoq_growth_pct", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "company_signals", sa.Column("news_distress_detected", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("company_signals", "news_distress_detected")
    op.drop_column("company_signals", "share_price_qoq_growth_pct")
    op.drop_column("company_signals", "news_tags")
    op.drop_column("company_signals", "news_summary_generated_at")
    op.drop_column("company_signals", "news_summary")

    op.drop_table("geocoded_addresses")

    op.drop_index("ix_product_fit_judgments_lookup", table_name="product_fit_judgments")
    op.drop_table("product_fit_judgments")
