from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.cards import ExhibitionCreate, ExhibitionOut
from app.services import exhibition_service

router = APIRouter(prefix="/exhibitions", tags=["exhibitions"])


@router.post("", status_code=201, response_model=ExhibitionOut)
def create_exhibition(
    data: ExhibitionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    exhibition = exhibition_service.create_exhibition(db, user, data)
    return ExhibitionOut.model_validate(exhibition)


@router.get("", response_model=list[ExhibitionOut])
def list_exhibitions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    exhibitions = exhibition_service.list_exhibitions(db, user)
    return [ExhibitionOut.model_validate(e) for e in exhibitions]
