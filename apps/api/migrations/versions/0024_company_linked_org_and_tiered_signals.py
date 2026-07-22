"""company linked_org_id + tiered company_signals clocks + visiting_cards.company_enriched_at

See .claude/specs/24-company-linkage-tiered-expiry.md:
- companies.linked_org_id tags a scanned company as itself being a
  registered DASHR org (nullable, one-way, never scoped/tenant data).
- company_signals.updated_at is replaced by two independent freshness
  clocks, factual_fetched_at (180-day TTL) and dynamic_fetched_at (90-day
  TTL) — pre-existing rows are backfilled from their old updated_at value,
  since every row enriched under the old one-shot model had both halves
  fetched together.
- visiting_cards.company_enriched_at anchors the new, separate per-lead
  30-day billed cooldown — unrelated to the two clocks above.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-22

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("linked_org_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_companies_linked_org_id_organizations",
        "companies",
        "organizations",
        ["linked_org_id"],
        ["org_id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_companies_linked_org_id", "companies", ["linked_org_id"])

    op.add_column(
        "company_signals",
        sa.Column("factual_fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "company_signals",
        sa.Column("dynamic_fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE company_signals SET factual_fetched_at = updated_at, "
        "dynamic_fetched_at = updated_at WHERE updated_at IS NOT NULL"
    )
    op.drop_column("company_signals", "updated_at")

    op.add_column(
        "visiting_cards",
        sa.Column("company_enriched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("visiting_cards", "company_enriched_at")

    op.add_column(
        "company_signals",
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.execute("UPDATE company_signals SET updated_at = COALESCE(factual_fetched_at, dynamic_fetched_at, now())")
    op.drop_column("company_signals", "dynamic_fetched_at")
    op.drop_column("company_signals", "factual_fetched_at")

    op.drop_index("ix_companies_linked_org_id", table_name="companies")
    op.drop_constraint("fk_companies_linked_org_id_organizations", "companies", type_="foreignkey")
    op.drop_column("companies", "linked_org_id")
