from sqlalchemy import Select, select
from sqlalchemy.orm import InstrumentedAttribute

from app.models.user import User


def scope_to_visible_users(
    stmt: Select, current_user: User, owner_user_id_column: InstrumentedAttribute
) -> Select:
    """Restricts a query on a user-owned table to what `current_user` may see.

    - org admin (`role == "admin"` and `org_id` set): every user in the same org
    - member or org-less user: only their own rows

    `owner_user_id_column` is the target table's `user_id` column (e.g.
    `VisitingCard.user_id` or `Exhibition.user_id`), passed explicitly so this
    helper stays table-agnostic — the single place the admin-sees-org-member
    join lives, reused by every service that needs the card-visibility rule
    established in 01-database-setup instead of reimplementing it per query.
    """
    if current_user.role == "admin" and current_user.org_id is not None:
        return stmt.where(
            owner_user_id_column.in_(
                select(User.user_id).where(User.org_id == current_user.org_id)
            )
        )
    return stmt.where(owner_user_id_column == current_user.user_id)
