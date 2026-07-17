import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_current_admin, get_current_user, get_db, get_invite_email_provider
from app.models.user import User
from app.schemas.auth import UserOut
from app.schemas.orgs import InviteCreate, InviteOut, InvitePreviewOut, MyInviteOut, OrgMemberOut
from app.services import auth_service, org_service
from app.services.exceptions import (
    AlreadyInOrganizationError,
    CannotTargetSelfError,
    DuplicatePendingInviteError,
    InviteEmailMismatchError,
    InviteNotFoundError,
    UserNotFoundError,
)
from app.services.invite_email_provider import InviteEmailProvider

router = APIRouter(prefix="/orgs", tags=["orgs"])


@router.get("/my-invites", response_model=list[MyInviteOut])
def list_my_invites(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return [
        MyInviteOut(invite_id=invite.invite_id, org_name=org_name, token=invite.token, expires_at=invite.expires_at)
        for invite, org_name in org_service.list_my_invites(db, user)
    ]


@router.get("/invites/{token}", response_model=InvitePreviewOut)
def preview_invite(token: str, db: Session = Depends(get_db)):
    try:
        invite, org_name = org_service.get_invite_preview(db, token)
    except InviteNotFoundError:
        raise HTTPException(status_code=404, detail="Invite not found")

    return InvitePreviewOut(org_name=org_name, invitee_email=invite.email, status=invite.status)


@router.post("/invites", status_code=201, response_model=InviteOut)
def create_invite(
    data: InviteCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    provider: InviteEmailProvider = Depends(get_invite_email_provider),
):
    try:
        invite = org_service.create_invite(db, admin, data.email, provider)
    except DuplicatePendingInviteError:
        raise HTTPException(
            status_code=409, detail="A pending invite already exists for this email"
        )

    return InviteOut.model_validate(invite)


@router.get("/invites", response_model=list[InviteOut])
def list_invites(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    return [InviteOut.model_validate(invite) for invite in org_service.list_invites(db, admin)]


@router.delete("/invites/{invite_id}", status_code=204)
def revoke_invite(
    invite_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    try:
        org_service.revoke_invite(db, admin, invite_id)
    except InviteNotFoundError:
        raise HTTPException(status_code=404, detail="Invite not found")


@router.post("/invites/{token}/accept", response_model=UserOut)
def accept_invite(
    token: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        updated_user = org_service.accept_invite(db, user, token)
    except InviteNotFoundError:
        raise HTTPException(status_code=404, detail="Invite not found")
    except InviteEmailMismatchError:
        raise HTTPException(
            status_code=403, detail="Invite email does not match your account"
        )
    except AlreadyInOrganizationError:
        raise HTTPException(status_code=409, detail="You already belong to an organization")

    return auth_service.to_user_out(db, updated_user)


@router.get("/members", response_model=list[OrgMemberOut])
def list_members(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    return [OrgMemberOut.model_validate(member) for member in org_service.list_members(db, admin)]


@router.patch("/members/{user_id}/deactivate", response_model=OrgMemberOut)
def deactivate_member(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    try:
        target = org_service.deactivate_member(db, admin, user_id)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except CannotTargetSelfError:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    return OrgMemberOut.model_validate(target)


@router.patch("/members/{user_id}/reactivate", response_model=OrgMemberOut)
def reactivate_member(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    try:
        target = org_service.reactivate_member(db, admin, user_id)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")

    return OrgMemberOut.model_validate(target)


@router.post("/members/{user_id}/make-admin", status_code=204)
def make_admin(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    try:
        org_service.make_admin(db, admin, user_id)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except CannotTargetSelfError:
        raise HTTPException(status_code=400, detail="You are already the admin")
