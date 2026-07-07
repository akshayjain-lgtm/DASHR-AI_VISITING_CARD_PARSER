import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.cards import (
    BulkUploadCardSummary,
    BulkUploadResponse,
    CardDetailOut,
    CardOut,
    CardProcessRequest,
    CardProcessResponse,
)
from app.services import card_service
from app.services.exceptions import (
    BatchTooLargeError,
    CardNotFoundError,
    EmptyBatchError,
    ExhibitionNotFoundError,
    FileTooLargeError,
    InvalidReprocessStateError,
    UnsupportedFileTypeError,
)

router = APIRouter(prefix="/cards", tags=["cards"])


@router.post("/bulk-upload", status_code=201, response_model=BulkUploadResponse)
def bulk_upload(
    exhibition_id: uuid.UUID | None = Form(default=None),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        cards = card_service.bulk_upload_cards(db, user, exhibition_id, files)
    except ExhibitionNotFoundError:
        raise HTTPException(status_code=404, detail="Exhibition not found")
    except (
        EmptyBatchError,
        UnsupportedFileTypeError,
        FileTooLargeError,
        BatchTooLargeError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return BulkUploadResponse(
        batch_size=len(cards),
        cards=[
            BulkUploadCardSummary(
                card_id=c.card_id,
                original_filename=c.original_filename,
                status=c.status,
                exhibition_id=c.exhibition_id,
            )
            for c in cards
        ],
    )


@router.post("/process", response_model=CardProcessResponse)
def process_cards(
    body: CardProcessRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    count = card_service.enqueue_processing(db, user, body.exhibition_id)
    return CardProcessResponse(enqueued_count=count)


@router.get("", response_model=list[CardOut])
def list_cards(
    exhibition_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cards = card_service.list_cards(db, user, exhibition_id, status, limit, offset)
    return [CardOut.model_validate(c) for c in cards]


@router.get("/{card_id}", response_model=CardDetailOut)
def get_card(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        detail = card_service.get_card_detail(db, user, card_id)
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")
    return CardDetailOut.model_validate(detail)


@router.post("/{card_id}/reprocess", response_model=CardOut)
def reprocess_card(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        card = card_service.reprocess_card(db, user, card_id)
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")
    except InvalidReprocessStateError:
        raise HTTPException(status_code=409, detail="Card is not in a failed state")
    return CardOut.model_validate(card)
