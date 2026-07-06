import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.exhibition import Exhibition
from app.models.user import User
from app.schemas.cards import ExhibitionCreate
from app.services.exceptions import ExhibitionNotFoundError
from app.services.visibility import scope_to_visible_users


def create_exhibition(
    db: Session, current_user: User, data: ExhibitionCreate
) -> Exhibition:
    exhibition = Exhibition(
        name=data.name,
        location=data.location,
        start_date=data.start_date,
        end_date=data.end_date,
        user_id=current_user.user_id,
    )
    db.add(exhibition)
    db.commit()
    db.refresh(exhibition)
    return exhibition


def list_exhibitions(db: Session, current_user: User) -> list[Exhibition]:
    stmt = scope_to_visible_users(select(Exhibition), current_user, Exhibition.user_id)
    stmt = stmt.order_by(Exhibition.created_at.desc())
    return list(db.scalars(stmt))


def get_visible_exhibition(
    db: Session, current_user: User, exhibition_id: uuid.UUID
) -> Exhibition:
    """Raises ExhibitionNotFoundError if the exhibition doesn't exist or
    isn't visible to `current_user` under the admin-sees-org-member rule —
    reused by card_service.py for the bulk-upload ownership check."""
    stmt = scope_to_visible_users(select(Exhibition), current_user, Exhibition.user_id)
    stmt = stmt.where(Exhibition.exhibition_id == exhibition_id)
    exhibition = db.scalar(stmt)
    if exhibition is None:
        raise ExhibitionNotFoundError()
    return exhibition
