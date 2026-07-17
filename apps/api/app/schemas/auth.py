import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

INDIA_PHONE_PATTERN = r"^\+91[6-9]\d{9}$"


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone_no: str = Field(pattern=INDIA_PHONE_PATTERN)
    password: str = Field(min_length=8)
    # Present -> creates a new Organization and makes the signer its admin.
    # Absent/blank -> signer stays org-less, matching pre-existing behavior.
    company_name: str | None = Field(default=None, max_length=200)


class SignupResponse(BaseModel):
    user_id: uuid.UUID
    phone_no: str


class VerifyOtpRequest(BaseModel):
    user_id: uuid.UUID
    otp_code: str = Field(min_length=4, max_length=4)


class ResendOtpRequest(BaseModel):
    user_id: uuid.UUID


class LoginRequest(BaseModel):
    email: EmailStr
    # No min_length (unlike SignupRequest.password): a login attempt must
    # still hit the generic credential-check path even for a legacy/short
    # password, not get short-circuited by a schema-level 422. max_length
    # guards against feeding an oversized string into bcrypt hashing.
    password: str = Field(max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    name: str | None
    email: str
    phone_no: str | None
    org_id: uuid.UUID | None
    org_name: str | None
    role: str | None
    phone_verified: bool
    is_active: bool
    admin_name: str | None
    admin_email: str | None
