from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, get_support_query_email_provider
from app.models.user import User
from app.schemas.feedback import FeedbackCreate, SupportQueryCreate, SupportQueryOut
from app.services import feedback_service
from app.services.support_query_email_provider import SupportQueryEmailProvider

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", status_code=204)
def submit_feedback(
    data: FeedbackCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    feedback_service.create_feedback(db, user, data)


@router.post("/queries", response_model=SupportQueryOut)
def submit_query(
    data: SupportQueryCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    provider: SupportQueryEmailProvider = Depends(get_support_query_email_provider),
):
    return feedback_service.create_support_query(db, user, data, provider)
