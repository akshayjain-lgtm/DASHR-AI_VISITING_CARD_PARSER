"""users.phone_verified + partial unique index, phone_otp_verifications table

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "phone_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_index(
        "uq_users_phone_no_verified", "users", ["phone_no"], unique=True,
        postgresql_where=sa.text("phone_verified = true"),
    )

    op.create_table(
        "phone_otp_verifications",
        sa.Column("otp_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_no", sa.String(), nullable=False),
        sa.Column("otp_code_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("otp_id", name="pk_phone_otp_verifications"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"],
            name="fk_phone_otp_verifications_user_id_users", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_phone_otp_verifications_user_id", "phone_otp_verifications", ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_phone_otp_verifications_user_id", table_name="phone_otp_verifications")
    op.drop_table("phone_otp_verifications")
    op.drop_index("uq_users_phone_no_verified", table_name="users")
    op.drop_column("users", "phone_verified")
