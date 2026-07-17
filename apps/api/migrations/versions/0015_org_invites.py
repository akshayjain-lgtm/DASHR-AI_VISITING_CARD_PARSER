"""org_invites

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_invites",
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.org_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column(
            "invited_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column(
            "accepted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("accepted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_check_constraint(
        "ck_org_invites_role_valid", "org_invites", "role = 'member'"
    )
    op.create_check_constraint(
        "ck_org_invites_status_valid",
        "org_invites",
        "status IN ('pending', 'accepted', 'revoked', 'expired')",
    )
    op.create_index(
        "uq_org_invites_org_email_pending",
        "org_invites",
        ["org_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_org_invites_org_email_pending", table_name="org_invites")
    op.drop_table("org_invites")
