"""Read-only aggregations backing the Dashboard's analytics layer (CLAUDE.md
step 8, "Analytics dashboard"). Every aggregation is scoped through
scope_to_visible_users, identical to card_service.list_cards — nothing here
bypasses tenant/visibility rules.

Score-bucket cutoffs (_SCORE_BUCKET_HIGH_MIN/_SCORE_BUCKET_MEDIUM_MIN below)
are the single source of truth for what counts as a high/medium/low/unscored
lead — the frontend has no client-side re-derivation of these thresholds, it
only renders whatever bucket counts this module returns.
"""
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.exhibition import Exhibition
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services.region_classification import classify_region
from app.services.visibility import scope_to_visible_users

_SCORE_BUCKET_HIGH_MIN = 80
_SCORE_BUCKET_MEDIUM_MIN = 60
_UNCLASSIFIED_LABEL = "Unclassified"


@dataclass(frozen=True)
class AnalyticsFilters:
    """The exhibition/date-range/uploader slice every aggregation below is
    filtered to — bundled into one object instead of loose positional params
    repeated across all six aggregators, so adding a filter or reordering
    one doesn't require touching every function signature."""

    exhibition_ids: list[uuid.UUID] | None = None
    start_date: date | None = None
    end_date: date | None = None
    # Admin-only "uploaded by" filter — narrows within whatever
    # scope_to_visible_users already allows, mirroring card_service.list_cards's
    # existing user_id param. Safe to accept from any caller: a non-admin's
    # query is already self-scoped, so this can only narrow to "self or
    # nothing", never widen visibility.
    user_id: uuid.UUID | None = None


def _apply_shared_filters(stmt: Select, current_user: User, filters: AnalyticsFilters) -> Select:
    """The one place AnalyticsFilters is applied, so all six aggregations
    below stay identically filtered."""
    stmt = scope_to_visible_users(stmt, current_user, VisitingCard.user_id)
    if filters.user_id is not None:
        stmt = stmt.where(VisitingCard.user_id == filters.user_id)
    if filters.exhibition_ids:
        stmt = stmt.where(VisitingCard.exhibition_id.in_(filters.exhibition_ids))
    if filters.start_date is not None:
        stmt = stmt.where(VisitingCard.created_at >= filters.start_date)
    if filters.end_date is not None:
        # created_at is TIMESTAMPTZ — a plain `<= end_date` would exclude
        # any time-of-day after midnight on end_date itself, so use an
        # exclusive upper bound one day later instead.
        stmt = stmt.where(VisitingCard.created_at < filters.end_date + timedelta(days=1))
    return stmt


def _lead_volume(db: Session, current_user: User, filters: AnalyticsFilters) -> list[dict]:
    day = func.date(VisitingCard.created_at).label("day")
    stmt = _apply_shared_filters(
        select(day, func.count(VisitingCard.card_id).label("count")),
        current_user,
        filters,
    ).group_by(day).order_by(day)
    rows = db.execute(stmt).all()
    return [{"date": r.day, "count": r.count} for r in rows]


def _industry_mix(db: Session, current_user: User, filters: AnalyticsFilters) -> list[dict]:
    industry_label = case(
        (or_(Company.industry.is_(None), Company.industry == ""), _UNCLASSIFIED_LABEL),
        else_=Company.industry,
    ).label("industry")
    stmt = _apply_shared_filters(
        select(industry_label, func.count(VisitingCard.card_id).label("count")).outerjoin(
            Company, VisitingCard.company_id == Company.company_id
        ),
        current_user,
        filters,
    ).group_by(industry_label).order_by(func.count(VisitingCard.card_id).desc())
    rows = db.execute(stmt).all()
    return [{"industry": r.industry, "count": r.count} for r in rows]


def _score_distribution(db: Session, current_user: User, filters: AnalyticsFilters) -> dict:
    bucket = case(
        (VisitingCard.lead_score.is_(None), "unscored"),
        (VisitingCard.lead_score >= _SCORE_BUCKET_HIGH_MIN, "high"),
        (VisitingCard.lead_score >= _SCORE_BUCKET_MEDIUM_MIN, "medium"),
        else_="low",
    ).label("bucket")
    stmt = _apply_shared_filters(
        select(bucket, func.count(VisitingCard.card_id).label("count")),
        current_user,
        filters,
    ).group_by(bucket)
    rows = db.execute(stmt).all()
    counts = {"high": 0, "medium": 0, "low": 0, "unscored": 0}
    for r in rows:
        counts[r.bucket] = r.count
    return counts


def _exhibition_performance(db: Session, current_user: User, filters: AnalyticsFilters) -> list[dict]:
    # Inner join: a card with no exhibition has nothing to attribute
    # performance to, so it's correctly excluded from this aggregation only
    # (the other five operate on all of the caller's visible cards).
    # avg_score intentionally omitted for the time being — see
    # .claude/specs/16-dashboard-analytics.md's "Overview" section.
    stmt = _apply_shared_filters(
        select(
            Exhibition.exhibition_id,
            Exhibition.name.label("exhibition_name"),
            func.count(VisitingCard.card_id).label("lead_count"),
        ).join(Exhibition, VisitingCard.exhibition_id == Exhibition.exhibition_id),
        current_user,
        filters,
    ).group_by(Exhibition.exhibition_id, Exhibition.name)
    rows = db.execute(stmt).all()
    return [
        {
            "exhibition_id": r.exhibition_id,
            "exhibition_name": r.exhibition_name,
            "lead_count": r.lead_count,
        }
        for r in rows
    ]


def _role_mix(db: Session, current_user: User, filters: AnalyticsFilters) -> list[dict]:
    role_label = case(
        (VisitingCard.designation_level.is_(None), _UNCLASSIFIED_LABEL),
        else_=VisitingCard.designation_level,
    ).label("role")
    stmt = _apply_shared_filters(
        select(role_label, func.count(VisitingCard.card_id).label("count")),
        current_user,
        filters,
    ).group_by(role_label).order_by(func.count(VisitingCard.card_id).desc())
    rows = db.execute(stmt).all()
    return [{"role": r.role, "count": r.count} for r in rows]


def _region_mix(db: Session, current_user: User, filters: AnalyticsFilters) -> list[dict]:
    # No Company.hq_city/hq_country writer exists (always NULL), and this
    # deliberately doesn't add one — region is classified in Python from
    # the card's free-text address at query time, not persisted. See
    # .claude/specs/16-dashboard-analytics.md's "Region classification".
    stmt = _apply_shared_filters(select(VisitingCard.address), current_user, filters)
    addresses = db.execute(stmt).scalars().all()
    counts: dict[str, int] = {}
    for address in addresses:
        region = classify_region(address)
        counts[region] = counts.get(region, 0) + 1
    return sorted(
        ({"region": region, "count": count} for region, count in counts.items()),
        key=lambda row: row["count"],
        reverse=True,
    )


def get_dashboard_analytics(
    db: Session,
    current_user: User,
    exhibition_ids: list[uuid.UUID] | None,
    start_date: date | None,
    end_date: date | None,
    user_id: uuid.UUID | None = None,
) -> dict:
    filters = AnalyticsFilters(
        exhibition_ids=exhibition_ids, start_date=start_date, end_date=end_date, user_id=user_id
    )
    return {
        "lead_volume": _lead_volume(db, current_user, filters),
        "industry_mix": _industry_mix(db, current_user, filters),
        "score_distribution": _score_distribution(db, current_user, filters),
        "exhibition_performance": _exhibition_performance(db, current_user, filters),
        "role_mix": _role_mix(db, current_user, filters),
        "region_mix": _region_mix(db, current_user, filters),
    }
