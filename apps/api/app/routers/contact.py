from fastapi import APIRouter, Depends

from app.deps import get_contact_email_provider
from app.schemas.contact import ContactEnquiryCreate
from app.services.contact_email_provider import ContactEmailProvider

router = APIRouter(prefix="/contact", tags=["contact"])


@router.post("", status_code=204)
def submit_enquiry(
    data: ContactEnquiryCreate,
    provider: ContactEmailProvider = Depends(get_contact_email_provider),
):
    provider.send(data.name, data.phone_no, data.email, data.query)
