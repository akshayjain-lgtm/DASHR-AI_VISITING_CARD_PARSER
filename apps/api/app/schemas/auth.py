import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

INDIA_PHONE_PATTERN = r"^\+91[6-9]\d{9}$"


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone_no: str = Field(pattern=INDIA_PHONE_PATTERN)
    password: str = Field(min_length=8)


class SignupResponse(BaseModel):
    user_id: uuid.UUID
    phone_no: str


class VerifyOtpRequest(BaseModel):
    user_id: uuid.UUID
    otp_code: str = Field(min_length=6, max_length=6)


class ResendOtpRequest(BaseModel):
    user_id: uuid.UUID


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    name: str | None
    email: str
    phone_no: str | None
    org_id: uuid.UUID | None
    role: str | None
    phone_verified: bool
