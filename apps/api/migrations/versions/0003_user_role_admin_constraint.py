"""users.role (admin/member) + one-admin-per-org constraint

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-03

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(), nullable=True))

    op.create_check_constraint(
        "ck_users_role_valid", "users", "role IS NULL OR role IN ('admin', 'member')"
    )
    op.create_check_constraint(
        "ck_users_admin_requires_org", "users", "role <> 'admin' OR org_id IS NOT NULL"
    )
    op.create_index(
        "uq_users_org_admin", "users", ["org_id"], unique=True,
        postgresql_where=sa.text("role = 'admin'"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_org_admin", table_name="users")
    op.drop_constraint("ck_users_admin_requires_org", "users", type_="check")
    op.drop_constraint("ck_users_role_valid", "users", type_="check")
    op.drop_column("users", "role")
