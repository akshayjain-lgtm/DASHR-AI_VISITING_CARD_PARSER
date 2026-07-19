import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Invoice(Base):
    """Immutable record of one wallet recharge's invoice — generated once by
    services/invoicing.py and never updated or deleted afterward (CLAUDE.md:
    corrections are new adjustment entries, not edits to an issued invoice).

    Every bill-to/issuer/tax field here is a snapshot taken at generation
    time, not a live join to SellerProfile/User/billing.py's constants — so
    an invoice's PDF and this row always agree, forever, even after the
    source profile or GST rate changes later. org_id is denormalized from
    User.org_id purely so GET /invoices/org can filter by tenant without a
    join through users; billing/visibility scope is still user_id, mirroring
    Wallet/WalletTransaction (CLAUDE.md)."""

    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_user_id_issued_at", "user_id", "issued_at"),
        Index("ix_invoices_org_id_issued_at", "org_id", "issued_at"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    # Nullable only because User.org_id itself is nullable (mirrors that
    # column) — not a deliberate org_id-everywhere exemption.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="SET NULL")
    )
    wallet_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallet_transactions.wallet_transaction_id"),
        nullable=False,
        unique=True,
    )
    invoice_number: Mapped[str] = mapped_column(unique=True)
    sac_code: Mapped[str] = mapped_column(server_default=text("'9983'"))
    taxable_value_inr: Mapped[Decimal] = mapped_column(Numeric)
    cgst_rate_percent: Mapped[Decimal] = mapped_column(Numeric, server_default=text("9.00"))
    sgst_rate_percent: Mapped[Decimal] = mapped_column(Numeric, server_default=text("9.00"))
    cgst_amount_inr: Mapped[Decimal] = mapped_column(Numeric)
    sgst_amount_inr: Mapped[Decimal] = mapped_column(Numeric)
    total_inr: Mapped[Decimal] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(server_default=text("'INR'"))
    service_description: Mapped[str] = mapped_column(
        server_default=text(
            "'Cardex Recharge - For Visiting Card Parsing,Enrichment and Scoring'"
        )
    )
    bill_to_name: Mapped[str]
    bill_to_gst_no: Mapped[str | None]
    bill_to_billing_address: Mapped[str | None]
    issuer_name: Mapped[str]
    issuer_gst_no: Mapped[str]
    issuer_address: Mapped[str]
    terms_and_conditions: Mapped[str] = mapped_column(Text)
    pdf_storage_key: Mapped[str]
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
