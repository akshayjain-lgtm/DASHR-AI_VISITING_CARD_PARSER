"""org_invites case-insensitive email uniqueness

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The prior index was case-sensitive, letting an admin create two
    # "duplicate" pending invites to Foo@x.com and foo@x.com for the same
    # person — accept_invite/list_my_invites already compare emails
    # case-insensitively, so the uniqueness guard should match.
    op.drop_index("uq_org_invites_org_email_pending", table_name="org_invites")
    op.create_index(
        "uq_org_invites_org_email_pending",
        "org_invites",
        ["org_id", sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_org_invites_org_email_pending", table_name="org_invites")
    op.create_index(
        "uq_org_invites_org_email_pending",
        "org_invites",
        ["org_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
