import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.exhibition import Exhibition
from app.models.user import User
from app.schemas.cards import ExhibitionCreate
from app.services.exceptions import DuplicateExhibitionError, ExhibitionNotFoundError
from app.services.visibility import scope_to_visible_users


def create_exhibition(
    db: Session, current_user: User, data: ExhibitionCreate
) -> Exhibition:
    normalized_name = data.name.strip()

    # Dedupe scope is the whole organization (not just admins, and not just
    # the caller's own rows like scope_to_visible_users' member branch) —
    # two different members of the same org shouldn't be able to create the
    # same trade show twice just because they uploaded separate batches. An
    # org-less user has no org-mates to check against, so they're deduped
    # against their own exhibitions only.
    dedupe_stmt = select(Exhibition.exhibition_id).where(
        func.lower(Exhibition.name) == normalized_name.lower(),
        Exhibition.start_date == data.start_date,
    )
    if current_user.org_id is not None:
        dedupe_stmt = dedupe_stmt.where(
            Exhibition.user_id.in_(
                select(User.user_id).where(User.org_id == current_user.org_id)
            )
        )
    else:
        dedupe_stmt = dedupe_stmt.where(Exhibition.user_id == current_user.user_id)

    if db.scalar(dedupe_stmt) is not None:
        raise DuplicateExhibitionError()

    exhibition = Exhibition(
        name=normalized_name,
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
