from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.seller_profile import SellerProfile
from app.models.user import User
from app.schemas.profile import SellerProfileUpdate


def _find_profile(db: Session, current_user: User) -> SellerProfile | None:
    stmt = select(SellerProfile).where(SellerProfile.user_id == current_user.user_id)
    return db.scalar(stmt)


def get_or_empty_profile(db: Session, current_user: User) -> SellerProfile:
    """Returns current_user's seller_profiles row, or a transient (never
    db.add-ed, never committed) SellerProfile sentinel if they've never
    saved one. An unflushed mapped object has every unset column as None in
    Python, so the router can treat both cases identically."""
    profile = _find_profile(db, current_user)
    if profile is not None:
        return profile
    return SellerProfile(user_id=current_user.user_id)


def upsert_profile(db: Session, current_user: User, data: SellerProfileUpdate) -> SellerProfile:
    """Get-or-create current_user's seller_profiles row, then applies only
    the fields present in the request. exclude_unset=True is what makes an
    omitted field a no-op rather than a null-out."""
    profile = _find_profile(db, current_user)
    if profile is None:
        profile = SellerProfile(user_id=current_user.user_id)
        db.add(profile)

    # `name` is the one SellerProfileUpdate field that isn't a
    # seller_profiles column — it writes through to User.name so the
    # profile form and the account's own name stay a single field, not two
    # that can drift apart. Schema-level min_length=1 already guarantees
    # this is never an empty-string clear.
    fields = data.model_dump(exclude_unset=True)
    name = fields.pop("name", None)
    if name is not None:
        current_user.name = name

    # Relies on SellerProfileUpdate's remaining field names matching
    # SellerProfile's column names 1:1 — there's no compile-time check of
    # that, so a renamed column or schema field must be renamed in both
    # places together.
    for field, value in fields.items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)
    return profile
