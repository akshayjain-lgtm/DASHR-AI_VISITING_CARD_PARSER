import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.analytics import DashboardAnalyticsOut
from app.services import analytics as analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard", response_model=DashboardAnalyticsOut)
def get_dashboard_analytics(
    # A plain `list[uuid.UUID] | None = None` default is silently dropped
    # from FastAPI's query-param binding (confirmed via the generated
    # OpenAPI schema — the param never appeared at all); repeated
    # `?exhibition_ids=...&exhibition_ids=...` values need an explicit
    # Query() to be recognized as a multi-value query parameter.
    exhibition_ids: list[uuid.UUID] | None = Query(default=None),
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = analytics_service.get_dashboard_analytics(
        db, user, exhibition_ids, start_date, end_date
    )
    return DashboardAnalyticsOut.model_validate(result)
