from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.profile import SellerProfileOut, SellerProfileUpdate
from app.services import profile_service

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=SellerProfileOut)
def get_profile(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    profile = profile_service.get_or_empty_profile(db, user)
    return SellerProfileOut.model_validate(profile)


@router.put("", response_model=SellerProfileOut)
def update_profile(
    data: SellerProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    profile = profile_service.upsert_profile(db, user, data)
    return SellerProfileOut.model_validate(profile)
