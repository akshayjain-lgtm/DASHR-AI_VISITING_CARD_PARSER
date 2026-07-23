"""feedback support queries

Two new authenticated-app tables backing the Feedback page — see
.claude/specs/23-feedback-and-support.md. `feedback` stores open-ended
product feedback for later internal review only (no email). `support_queries`
stores "raise a query" submissions; support_query_ticket_seq backs
ticket_id generation (app code calls nextval(), never a column default),
mirroring invoice_number_seq so ticket numbering stays gap-free and safe
under concurrent submissions without locking the table itself.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-22

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column(
            "feedback_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("what_worked", sa.Text(), nullable=True),
        sa.Column("what_went_wrong", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("feedback_id", name="pk_feedback"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"],
            name="fk_feedback_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.org_id"],
            name="fk_feedback_org_id_organizations", ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "what_worked IS NOT NULL OR what_went_wrong IS NOT NULL",
            name="ck_feedback_at_least_one_field",
        ),
    )
    op.create_index("ix_feedback_user_id_created_at", "feedback", ["user_id", "created_at"])

    op.execute("CREATE SEQUENCE support_query_ticket_seq")
    op.create_table(
        "support_queries",
        sa.Column(
            "support_query_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("email_sent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("support_query_id", name="pk_support_queries"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"],
            name="fk_support_queries_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.org_id"],
            name="fk_support_queries_org_id_organizations", ondelete="SET NULL",
        ),
        sa.UniqueConstraint("ticket_id", name="uq_support_queries_ticket_id"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_support_queries_status_valid"),
    )
    op.create_index("ix_support_queries_user_id_created_at", "support_queries", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_support_queries_user_id_created_at", table_name="support_queries")
    op.drop_table("support_queries")
    op.execute("DROP SEQUENCE support_query_ticket_seq")

    op.drop_index("ix_feedback_user_id_created_at", table_name="feedback")
    op.drop_table("feedback")
