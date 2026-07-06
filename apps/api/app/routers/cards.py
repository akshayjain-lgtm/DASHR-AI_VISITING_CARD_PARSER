import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.cards import BulkUploadCardSummary, BulkUploadResponse, CardOut
from app.services import card_service
from app.services.exceptions import (
    BatchTooLargeError,
    EmptyBatchError,
    ExhibitionNotFoundError,
    FileTooLargeError,
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
