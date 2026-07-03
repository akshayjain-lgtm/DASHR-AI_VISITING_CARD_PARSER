"""initial schema: users, companies, exhibitions, visiting_cards, card_phones, card_emails, company_enrichment

Revision ID: 0001
Revises:
Create Date: 2026-07-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')

    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("phone_no", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "companies",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("normalized_name", sa.String(), nullable=True),
        sa.Column("domain", sa.String(), nullable=True),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column("industry", sa.String(), nullable=True),
        sa.Column("size_bucket", sa.String(), nullable=True),
        sa.Column("hq_city", sa.String(), nullable=True),
        sa.Column("hq_country", sa.String(), nullable=True),
        sa.Column("linkedin_url", sa.String(), nullable=True),
        sa.Column("enrichment_status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("enriched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("company_id", name="pk_companies"),
    )
    op.create_index(
        "uq_companies_domain", "companies", ["domain"], unique=True,
        postgresql_where=sa.text("domain IS NOT NULL"),
    )
    op.create_index("ix_companies_normalized_name", "companies", ["normalized_name"])

    op.create_table(
        "exhibitions",
        sa.Column("exhibition_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("exhibition_id", name="pk_exhibitions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_exhibitions_user_id_users"),
    )

    op.create_table(
        "visiting_cards",
        sa.Column("card_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("exhibition_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("job_title", sa.String(), nullable=True),
        sa.Column("designation_level", sa.String(), nullable=True),
        sa.Column("raw_ocr_text", sa.Text(), nullable=True),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("lead_score", sa.Numeric(), nullable=True),
        sa.Column("score_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("scored_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'new'"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("card_id", name="pk_visiting_cards"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_visiting_cards_user_id_users"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.company_id"], name="fk_visiting_cards_company_id_companies"),
        sa.ForeignKeyConstraint(["exhibition_id"], ["exhibitions.exhibition_id"], name="fk_visiting_cards_exhibition_id_exhibitions"),
    )

    op.create_table(
        "card_phones",
        sa.Column("phone_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_e164", sa.String(), nullable=True),
        sa.Column("phone_raw", sa.String(), nullable=True),
        sa.Column("phone_type", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("phone_id", name="pk_card_phones"),
        sa.ForeignKeyConstraint(
            ["card_id"], ["visiting_cards.card_id"],
            name="fk_card_phones_card_id_visiting_cards", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("card_id", "phone_e164", name="uq_card_phones_card_id_phone_e164"),
    )

    op.create_table(
        "card_emails",
        sa.Column("email_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("email_type", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("email_id", name="pk_card_emails"),
        sa.ForeignKeyConstraint(
            ["card_id"], ["visiting_cards.card_id"],
            name="fk_card_emails_card_id_visiting_cards", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("card_id", "email", name="uq_card_emails_card_id_email"),
    )

    op.create_table(
        "company_enrichment",
        sa.Column("enrichment_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("enrichment_id", name="pk_company_enrichment"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.company_id"],
            name="fk_company_enrichment_company_id_companies",
        ),
    )


def downgrade() -> None:
    op.drop_table("company_enrichment")
    op.drop_table("card_emails")
    op.drop_table("card_phones")
    op.drop_table("visiting_cards")
    op.drop_table("exhibitions")
    op.drop_index("ix_companies_normalized_name", table_name="companies")
    op.drop_index("uq_companies_domain", table_name="companies")
    op.drop_table("companies")
    op.drop_table("users")
