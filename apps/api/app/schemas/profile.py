import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SellerProfileOut(BaseModel):
    """Every field is Optional, including ones that are non-null once a row
    actually exists (revenue_currency, created_at, updated_at) — this schema
    also represents the "never saved a profile yet" sentinel state
    (profile_id: null and everything else null). `name` is the one exception:
    it is sourced from User.name (the account holder's name, set at signup),
    not from a seller_profiles column, so it is populated even when
    profile_id is null — the router fills it in separately from the current
    user, not via SellerProfile.model_validate."""

    model_config = ConfigDict(from_attributes=True)

    profile_id: uuid.UUID | None
    # Defaulted (unlike every other field here) because SellerProfile (the
    # ORM object model_validate reads from) has no `name` attribute at all —
    # the router always overwrites this via model_copy right after
    # validating, using the current user's own name.
    name: str | None = None
    designation: str | None
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
    # min_length=1 (unlike every other field here): this updates User.name,
    # which is required account-wide, not a seller_profiles column — an
    # empty string must be rejected outright, not accepted as a "clear".
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # min_length=1, mirroring `name`: mandatory once set, so a caller can
    # never blank it out with "". Omitting the key entirely still leaves an
    # existing stored value unchanged, same partial-update semantics as
    # every other field — enforcement of "must be filled in" lives in the
    # Settings form (blocks Save on empty), not here.
    designation: str | None = Field(default=None, min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=200)
    product_lines: str | None = Field(default=None, max_length=2000)
    last_year_revenue: Decimal | None = Field(default=None, ge=0)
    revenue_currency: str | None = Field(default=None, max_length=10)
    target_customer_description: str | None = Field(default=None, max_length=2000)
    target_regions: str | None = Field(default=None, max_length=500)
    gst_no: str | None = Field(default=None, max_length=20)
    billing_address: str | None = Field(default=None, max_length=500)
