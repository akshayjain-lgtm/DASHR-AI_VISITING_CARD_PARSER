"""archive_uploads

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "archive_uploads",
        sa.Column(
            "archive_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("exhibition_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("container_type", sa.String(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), server_default="processing", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("archive_id", name="pk_archive_uploads"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name="fk_archive_uploads_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["exhibition_id"],
            ["exhibitions.exhibition_id"],
            name="fk_archive_uploads_exhibition_id_exhibitions",
        ),
    )
    op.create_index(
        "ix_archive_uploads_user_id_status", "archive_uploads", ["user_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_archive_uploads_user_id_status", table_name="archive_uploads")
    op.drop_table("archive_uploads")
