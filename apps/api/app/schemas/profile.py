import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SellerProfileOut(BaseModel):
    """Every field is Optional, including ones that are non-null once a row
    actually exists (revenue_currency, created_at, updated_at) — this schema
    also represents the "never saved a profile yet" sentinel state
    (profile_id: null and everything else null)."""

    model_config = ConfigDict(from_attributes=True)

    profile_id: uuid.UUID | None
    company_name: str | None
    industry: str | None
    product_lines: str | None
    last_year_revenue: Decimal | None
    revenue_currency: str | None
    target_customer_description: str | None
    target_regions: str | None
    gst_no: str | None
    billing_address: str | None
    created_at: datetime | None
    updated_at: datetime | None


class SellerProfileUpdate(BaseModel):
    company_name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=200)
    product_lines: str | None = Field(default=None, max_length=2000)
    last_year_revenue: Decimal | None = Field(default=None, ge=0)
    revenue_currency: str | None = Field(default=None, max_length=10)
    target_customer_description: str | None = Field(default=None, max_length=2000)
    target_regions: str | None = Field(default=None, max_length=500)
    gst_no: str | None = Field(default=None, max_length=20)
    billing_address: str | None = Field(default=None, max_length=500)
