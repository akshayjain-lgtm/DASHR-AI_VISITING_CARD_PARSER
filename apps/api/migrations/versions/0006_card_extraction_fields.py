"""visiting_cards extraction fields: website, address, products_offered,
upload_batch_id, batch_sequence, merged_into_card_id, extraction_error,
processed_at

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visiting_cards", sa.Column("website", sa.Text(), nullable=True))
    op.add_column("visiting_cards", sa.Column("address", sa.Text(), nullable=True))
    op.add_column(
        "visiting_cards", sa.Column("products_offered", sa.Text(), nullable=True)
    )
    op.add_column(
        "visiting_cards",
        sa.Column("upload_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "visiting_cards", sa.Column("batch_sequence", sa.Integer(), nullable=True)
    )
    op.add_column(
        "visiting_cards",
        sa.Column("merged_into_card_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "visiting_cards", sa.Column("extraction_error", sa.Text(), nullable=True)
    )
    op.add_column(
        "visiting_cards",
        sa.Column(
            "processed_at", sa.TIMESTAMP(timezone=True), nullable=True,
        ),
    )

    op.create_index(
        "ix_visiting_cards_upload_batch_id", "visiting_cards", ["upload_batch_id"],
    )
    op.create_foreign_key(
        "fk_visiting_cards_merged_into_card_id_visiting_cards",
        "visiting_cards", "visiting_cards",
        ["merged_into_card_id"], ["card_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_visiting_cards_merged_into_card_id_visiting_cards",
        "visiting_cards", type_="foreignkey",
    )
    op.drop_index("ix_visiting_cards_upload_batch_id", table_name="visiting_cards")
    op.drop_column("visiting_cards", "processed_at")
    op.drop_column("visiting_cards", "extraction_error")
    op.drop_column("visiting_cards", "merged_into_card_id")
    op.drop_column("visiting_cards", "batch_sequence")
    op.drop_column("visiting_cards", "upload_batch_id")
    op.drop_column("visiting_cards", "products_offered")
    op.drop_column("visiting_cards", "address")
    op.drop_column("visiting_cards", "website")
