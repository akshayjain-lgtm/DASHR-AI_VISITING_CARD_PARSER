from pydantic import BaseModel, EmailStr, Field


class ContactEnquiryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_no: str = Field(min_length=1, max_length=20)
    email: EmailStr
    query: str = Field(min_length=1, max_length=2000)
