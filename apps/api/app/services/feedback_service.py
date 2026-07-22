import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.feedback import Feedback
from app.models.support_query import SupportQuery
from app.models.user import User
from app.schemas.feedback import FeedbackCreate, SupportQueryCreate
from app.services.support_query_email_provider import SupportQueryEmailProvider

logger = logging.getLogger("dashr.feedback")


def create_feedback(db: Session, user: User, data: FeedbackCreate) -> Feedback:
    feedback = Feedback(
        user_id=user.user_id,
        org_id=user.org_id,
        what_worked=data.what_worked,
        what_went_wrong=data.what_went_wrong,
    )
    db.add(feedback)
    db.commit()
    return feedback


def create_support_query(
    db: Session,
    user: User,
    data: SupportQueryCreate,
    provider: SupportQueryEmailProvider,
) -> SupportQuery:
    # A DB sequence (not SELECT COUNT(*)) is what makes concurrent ticket
    # creation collision-free — mirrors invoicing.py's invoice_number_seq.
    sequence_value = db.scalar(select(func.nextval("support_query_ticket_seq")))
    ticket_id = f"DASHR-TKT-{sequence_value:06d}"

    query = SupportQuery(
        user_id=user.user_id,
        org_id=user.org_id,
        ticket_id=ticket_id,
        subject=data.subject,
        message=data.message,
    )
    db.add(query)
    db.commit()
    db.refresh(query)

    # The row (and its ticket id) is already committed above — a failed
    # send must never cost the user their submission or their reference
    # number, just leave email_sent=False for a later resend.
    try:
        provider.send(ticket_id, user.name, user.email, data.subject, data.message)
        query.email_sent = True
        db.commit()
    except Exception:
        logger.exception("Failed to send support query email for ticket %s", ticket_id)

    return query
