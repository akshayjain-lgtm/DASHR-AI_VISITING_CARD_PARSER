import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.cards import (
    BulkUploadCardSummary,
    BulkUploadResponse,
    CardDetailOut,
    CardEnrichRequest,
    CardEnrichResponse,
    CardOut,
    CardProcessRequest,
    CardProcessResponse,
)
from app.services import card_service
from app.services.exceptions import (
    BatchTooLargeError,
    CardHasMergedChildrenError,
    CardHasNoCompanyError,
    CardNotFoundError,
    CardStateChangedError,
    CompanyNotEligibleForEnrichmentError,
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
    count = card_service.enqueue_processing(db, user, body.exhibition_id, body.card_ids)
    return CardProcessResponse(enqueued_count=count)


@router.post("/enrich-companies", response_model=CardEnrichResponse)
def enrich_companies(
    body: CardEnrichRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    enqueued, skipped = card_service.enqueue_enrichment(db, user, body.card_ids)
    return CardEnrichResponse(enqueued_count=enqueued, skipped_count=skipped)


@router.get("", response_model=list[CardOut])
def list_cards(
    exhibition_id: uuid.UUID | None = None,
    status: str | None = None,
    include_folded: bool = False,
    # True filters to cards with no exhibition_id (the "General capture"
    # bucket in the upload page's exhibition picker) — distinct from omitting
    # exhibition_id, which returns cards across every exhibition.
    unassigned: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cards = card_service.list_cards(
        db, user, exhibition_id, status, limit, offset, include_folded, unassigned
    )
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
    return CardOut.model_validate(card_service.to_card_out(db, card))


@router.post("/{card_id}/enrich-company", response_model=CardOut)
def enrich_company(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        card = card_service.enrich_company_now(db, user, card_id)
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")
    except CardHasNoCompanyError:
        raise HTTPException(status_code=400, detail="This card has no linked company to enrich")
    except CompanyNotEligibleForEnrichmentError:
        raise HTTPException(
            status_code=409, detail="Company enrichment has already been started or completed"
        )
    return CardOut.model_validate(card_service.to_card_out(db, card))


@router.delete("/{card_id}", status_code=204)
def delete_card(
    card_id: uuid.UUID,
    confirm_cascade: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        card_service.delete_card(db, user, card_id, confirm_cascade)
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")
    except CardHasMergedChildrenError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"This card has {exc.child_count} merged card"
                    f"{'s' if exc.child_count != 1 else ''} that will also be deleted."
                ),
                "child_count": exc.child_count,
            },
        )
    except CardStateChangedError:
        raise HTTPException(
            status_code=409,
            detail="This card changed while being deleted — please try again",
        )
