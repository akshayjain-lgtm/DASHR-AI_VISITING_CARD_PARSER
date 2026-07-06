"""visiting_cards.original_filename, exhibitions.created_at, card status index

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "visiting_cards",
        sa.Column("original_filename", sa.Text(), nullable=True),
    )
    op.add_column(
        "exhibitions",
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index(
        "ix_visiting_cards_user_id_status", "visiting_cards", ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_visiting_cards_user_id_status", table_name="visiting_cards")
    op.drop_column("exhibitions", "created_at")
    op.drop_column("visiting_cards", "original_filename")
