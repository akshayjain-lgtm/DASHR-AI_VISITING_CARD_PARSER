"""seller_profiles.company_name index

enrichment_service.match_linked_org (see
.claude/specs/24-company-linkage-tiered-expiry.md) scans this column on
every enrichment call, first-run and refresh alike, to match a scanned
Company against a registered org's declared company_name. Adds an index
ahead of that cost becoming real at scale.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-22

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_seller_profiles_company_name", "seller_profiles", ["company_name"])


def downgrade() -> None:
    op.drop_index("ix_seller_profiles_company_name", table_name="seller_profiles")
