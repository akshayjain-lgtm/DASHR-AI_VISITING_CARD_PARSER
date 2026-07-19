import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.user import User
from app.services import storage_service
from app.services.exceptions import InvoiceNotFoundError


def list_invoices(db: Session, user_id: uuid.UUID, limit: int, offset: int) -> list[Invoice]:
    """Self-only — deliberately never joins in an admin's org-wide view here
    (see get_visible_invoice/list_org_invoices for that split), mirroring
    Wallet/WalletTransaction's own user-scoped-not-org-scoped visibility
    rule (CLAUDE.md)."""
    stmt = (
        select(Invoice)
        .where(Invoice.user_id == user_id)
        .order_by(Invoice.issued_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt))


def list_org_invoices(db: Session, org_id: uuid.UUID, limit: int, offset: int) -> list[Invoice]:
    """Admin-only (enforced by the router's get_current_admin dependency,
    not here). Filters directly on Invoice.org_id — already denormalized
    onto the row at generation time, so no join through users is needed."""
    stmt = (
        select(Invoice)
        .where(Invoice.org_id == org_id)
        .order_by(Invoice.issued_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt))


def get_visible_invoice(db: Session, user: User, invoice_id: uuid.UUID) -> Invoice:
    """Visible to the invoice's own owner, or to an admin of its org_id —
    read-only visibility only (CLAUDE.md: admin visibility into another
    user's billing data never implies spend/credit authority over it).
    Raises InvoiceNotFoundError (mapped to 404, never 403) for both a
    missing row and one that exists but isn't visible to this caller, so a
    non-owner can't distinguish the two."""
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise InvoiceNotFoundError()
    is_owner = invoice.user_id == user.user_id
    is_org_admin = (
        user.role == "admin" and user.org_id is not None and invoice.org_id == user.org_id
    )
    if not is_owner and not is_org_admin:
        raise InvoiceNotFoundError()
    return invoice


def get_invoice_pdf(db: Session, user: User, invoice_id: uuid.UUID) -> tuple[Invoice, bytes]:
    """Visibility check + S3 fetch together, so routers/invoices.py never
    touches storage_service directly — every other router in this codebase
    reaches object storage only through a service module (e.g.
    card_service.py), never inline."""
    invoice = get_visible_invoice(db, user, invoice_id)
    pdf_bytes = storage_service.download_file(invoice.pdf_storage_key)
    return invoice, pdf_bytes
