"""field_corrections

Append-only audit table for user-made corrections to AI-extracted or
enriched card/company fields — see .claude/specs/20-field-correction.md.
Every row carries both original_value and corrected_value; never updated
or deleted.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-19

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_corrections",
        sa.Column(
            "correction_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("corrected_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_name", sa.String(), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("corrected_value", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("correction_id", name="pk_field_corrections"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.org_id"],
            name="fk_field_corrections_org_id_organizations", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"], ["visiting_cards.card_id"],
            name="fk_field_corrections_card_id_visiting_cards", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corrected_by_user_id"], ["users.user_id"],
            name="fk_field_corrections_corrected_by_user_id_users",
        ),
    )
    op.create_index("ix_field_corrections_org_id", "field_corrections", ["org_id"])
    op.create_index("ix_field_corrections_card_id", "field_corrections", ["card_id"])
    op.create_index("ix_field_corrections_field_name", "field_corrections", ["field_name"])


def downgrade() -> None:
    op.drop_index("ix_field_corrections_field_name", table_name="field_corrections")
    op.drop_index("ix_field_corrections_card_id", table_name="field_corrections")
    op.drop_index("ix_field_corrections_org_id", table_name="field_corrections")
    op.drop_table("field_corrections")
