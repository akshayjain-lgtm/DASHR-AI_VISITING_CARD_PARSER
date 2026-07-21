import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.cards import (
    BulkUploadCardSummary,
    BulkUploadResponse,
    CardBulkDeleteRequest,
    CardBulkDeleteResponse,
    CardDetailOut,
    CardEnrichRequest,
    CardEnrichResponse,
    CardExportRequest,
    CardFieldCorrectionRequest,
    CardOut,
    CardProcessRequest,
    CardProcessResponse,
    CardScoreRequest,
    CardScoreResponse,
)
from app.services import card_service, export_service
from app.services.exceptions import (
    BatchTooLargeError,
    CardAlreadyScoredError,
    CardHasMergedChildrenError,
    CardHasNoCompanyError,
    CardNotEligibleForScoringError,
    CardNotFoundError,
    CardStateChangedError,
    CompanyNotEligibleForEnrichmentError,
    CorrectionRateLimitedError,
    EmptyBatchError,
    ExhibitionNotFoundError,
    FieldCorrectionRecordNotFoundError,
    FileTooLargeError,
    InsufficientBalanceError,
    InvalidCorrectionValueError,
    InvalidReprocessStateError,
    NoOpCorrectionError,
    UnsupportedFileTypeError,
)

router = APIRouter(prefix="/cards", tags=["cards"])


def _cascade_confirmation_detail(exc: CardHasMergedChildrenError, subject: str) -> dict:
    """Builds the {message, child_count} 409 body shared by DELETE
    /cards/{card_id} and POST /cards/bulk-delete — subject is "This card"
    or "Your selection", the only wording difference between the two."""
    return {
        "message": (
            f"{subject} has {exc.child_count} merged card"
            f"{'s' if exc.child_count != 1 else ''} that will also be deleted."
        ),
        "child_count": exc.child_count,
    }


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
    enqueued, wallet_blocked = card_service.enqueue_processing(
        db, user, body.exhibition_id, body.card_ids
    )
    return CardProcessResponse(enqueued_count=enqueued, wallet_blocked_count=wallet_blocked)


@router.post("/enrich-companies", response_model=CardEnrichResponse)
def enrich_companies(
    body: CardEnrichRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    enqueued, skipped, wallet_blocked = card_service.enqueue_enrichment(db, user, body.card_ids)
    return CardEnrichResponse(
        enqueued_count=enqueued, skipped_count=skipped, wallet_blocked_count=wallet_blocked
    )


@router.post("/score", response_model=CardScoreResponse)
def score_cards(
    body: CardScoreRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    enqueued, skipped, wallet_blocked = card_service.enqueue_scoring(db, user, body.card_ids)
    return CardScoreResponse(
        enqueued_count=enqueued, skipped_count=skipped, wallet_blocked_count=wallet_blocked
    )


@router.post("/export")
def export_cards(
    body: CardExportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = card_service.export_cards(db, user, body.card_ids)
    csv_text = export_service.build_csv(rows)
    filename = f"dashr-leads-{date.today().isoformat()}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/bulk-delete", response_model=CardBulkDeleteResponse)
def bulk_delete_cards(
    body: CardBulkDeleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        deleted, skipped = card_service.bulk_delete_cards(
            db, user, body.card_ids, body.confirm_cascade
        )
    except CardHasMergedChildrenError as exc:
        raise HTTPException(
            status_code=409, detail=_cascade_confirmation_detail(exc, "Your selection")
        )
    except CardStateChangedError:
        raise HTTPException(
            status_code=409,
            detail="Your selection changed while being deleted — please try again",
        )
    return CardBulkDeleteResponse(deleted_count=deleted, skipped_count=skipped)


@router.get("", response_model=list[CardOut])
def list_cards(
    exhibition_id: uuid.UUID | None = None,
    status: str | None = None,
    include_folded: bool = False,
    # True filters to cards with no exhibition_id (the "General capture"
    # bucket in the upload page's exhibition picker) — distinct from omitting
    # exhibition_id, which returns cards across every exhibition.
    unassigned: bool = False,
    # Admin-only "uploaded by" filter on the upload page — narrows within
    # whatever scope_to_visible_users already allows, so a non-admin passing
    # someone else's id just gets an empty result, never a leak.
    user_id: uuid.UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cards = card_service.list_cards(
        db,
        user,
        exhibition_id,
        status,
        limit,
        offset,
        include_folded,
        unassigned,
        user_id,
        start_date,
        end_date,
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


@router.post("/{card_id}/corrections", response_model=CardDetailOut)
def correct_card_field(
    card_id: uuid.UUID,
    body: CardFieldCorrectionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        detail = card_service.correct_card_field(
            db, user, card_id, body.field_name, body.corrected_value, body.record_id
        )
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")
    except FieldCorrectionRecordNotFoundError:
        raise HTTPException(status_code=404, detail="Email/phone record not found on this card")
    except InvalidCorrectionValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Corrected value is invalid")
    except NoOpCorrectionError:
        raise HTTPException(status_code=400, detail="Corrected value must differ from the current value")
    except CardHasNoCompanyError:
        raise HTTPException(status_code=400, detail="This card has no linked company")
    except CorrectionRateLimitedError:
        raise HTTPException(
            status_code=429,
            detail="Too many IndiaMART URL corrections — please wait a minute and try again",
        )
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
    except InsufficientBalanceError:
        raise HTTPException(
            status_code=402,
            detail="Wallet balance too low to reprocess this card — recharge your wallet to continue",
        )
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
    except InsufficientBalanceError:
        raise HTTPException(
            status_code=402,
            detail="Wallet balance too low to enrich this company — recharge your wallet to continue",
        )
    return CardOut.model_validate(card_service.to_card_out(db, card))


@router.post("/{card_id}/score", response_model=CardOut)
def score_card(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        card = card_service.score_card_now(db, user, card_id)
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found")
    except CardNotEligibleForScoringError:
        raise HTTPException(
            status_code=409, detail="Card must be extracted before it can be scored"
        )
    except CardAlreadyScoredError:
        raise HTTPException(status_code=409, detail="Card has already been scored")
    except InsufficientBalanceError:
        raise HTTPException(
            status_code=402,
            detail="Wallet balance too low to score this card — recharge your wallet to continue",
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
            status_code=409, detail=_cascade_confirmation_detail(exc, "This card")
        )
    except CardStateChangedError:
        raise HTTPException(
            status_code=409,
            detail="This card changed while being deleted — please try again",
        )
