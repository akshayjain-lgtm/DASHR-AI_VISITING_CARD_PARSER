import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.deps import get_current_admin, get_current_user, get_db
from app.models.user import User
from app.schemas.invoice import InvoiceOut
from app.services import invoice_service
from app.services.exceptions import InvoiceNotFoundError

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.get("", response_model=list[InvoiceOut])
def list_invoices(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    invoices = invoice_service.list_invoices(db, user.user_id, limit, offset)
    return [InvoiceOut.model_validate(i) for i in invoices]


# Declared before GET "/{invoice_id}" so FastAPI's path matching doesn't
# swallow "/org" as an {invoice_id} value.
@router.get("/org", response_model=list[InvoiceOut])
def list_org_invoices(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    invoices = invoice_service.list_org_invoices(db, admin.org_id, limit, offset)
    return [InvoiceOut.model_validate(i) for i in invoices]


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        invoice = invoice_service.get_visible_invoice(db, user, invoice_id)
    except InvoiceNotFoundError:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return InvoiceOut.model_validate(invoice)


@router.get("/{invoice_id}/pdf")
def get_invoice_pdf(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        invoice, pdf_bytes = invoice_service.get_invoice_pdf(db, user, invoice_id)
    except InvoiceNotFoundError:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{invoice.invoice_number}.pdf"'},
    )
