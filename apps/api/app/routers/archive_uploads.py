import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.archive_uploads import ArchiveUploadOut
from app.services import archive_upload_service
from app.services.exceptions import (
    ArchiveNotFoundError,
    BatchTooLargeError,
    CorruptArchiveError,
    EmptyBatchError,
    ExhibitionNotFoundError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)

router = APIRouter(prefix="/archive-uploads", tags=["archive-uploads"])


@router.post("", status_code=201, response_model=ArchiveUploadOut)
def create_archive_upload(
    exhibition_id: uuid.UUID | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        archive = archive_upload_service.create_archive_upload(db, user, exhibition_id, file)
    except ExhibitionNotFoundError:
        raise HTTPException(status_code=404, detail="Exhibition not found")
    except (
        EmptyBatchError,
        UnsupportedFileTypeError,
        FileTooLargeError,
        BatchTooLargeError,
        CorruptArchiveError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ArchiveUploadOut.model_validate(archive)


@router.get("/{archive_id}", response_model=ArchiveUploadOut)
def get_archive_upload(
    archive_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        archive = archive_upload_service.get_visible_archive_upload(db, user, archive_id)
    except ArchiveNotFoundError:
        raise HTTPException(status_code=404, detail="Archive upload not found")
    return ArchiveUploadOut.model_validate(archive)
