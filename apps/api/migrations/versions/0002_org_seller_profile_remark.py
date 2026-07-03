"""organizations, seller_profiles, users.org_id, visiting_cards.special_remark

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("org_id", name="pk_organizations"),
    )

    op.add_column("users", sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_users_org_id_organizations", "users", "organizations",
        ["org_id"], ["org_id"], ondelete="SET NULL",
    )

    op.create_table(
        "seller_profiles",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("industry", sa.String(), nullable=True),
        sa.Column("product_lines", sa.String(), nullable=True),
        sa.Column("last_year_revenue", sa.Numeric(), nullable=True),
        sa.Column("revenue_currency", sa.String(), server_default=sa.text("'INR'"), nullable=False),
        sa.Column("target_customer_description", sa.String(), nullable=True),
        sa.Column("target_regions", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("profile_id", name="pk_seller_profiles"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_seller_profiles_user_id_users"),
        sa.UniqueConstraint("user_id", name="uq_seller_profiles_user_id"),
    )

    op.add_column("visiting_cards", sa.Column("special_remark", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("visiting_cards", "special_remark")
    op.drop_table("seller_profiles")
    op.drop_constraint("fk_users_org_id_organizations", "users", type_="foreignkey")
    op.drop_column("users", "org_id")
    op.drop_table("organizations")
