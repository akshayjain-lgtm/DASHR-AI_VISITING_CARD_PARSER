import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID | None
    wallet_transaction_id: uuid.UUID
    invoice_number: str
    sac_code: str
    taxable_value_inr: Decimal
    cgst_rate_percent: Decimal
    sgst_rate_percent: Decimal
    cgst_amount_inr: Decimal
    sgst_amount_inr: Decimal
    total_inr: Decimal
    currency: str
    service_description: str
    bill_to_name: str
    bill_to_gst_no: str | None
    bill_to_billing_address: str | None
    issuer_name: str
    issuer_gst_no: str
    issuer_address: str
    terms_and_conditions: str
    # pdf_storage_key deliberately excluded — an internal S3 key, not part
    # of the API contract; the PDF is fetched via GET /invoices/{id}/pdf.
    issued_at: datetime
