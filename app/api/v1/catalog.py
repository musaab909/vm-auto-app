"""Public catalog: plans, regions, images and SSH keys."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from app.api.deps import CurrentAccount, CurrentUser, DbSession
from app.models.catalog import OsImage, Plan, PlanRegion, Region
from app.models.identity import SshKey
from app.schemas import ImageOut, PlanOut, RegionOut, SshKeyCreate, SshKeyOut
from app.security import ssh_key_fingerprint
from app.services import audit
from app.services.errors import ConflictError, NotFoundError, ValidationError

router = APIRouter(tags=["catalog"])


@router.get("/plans", response_model=list[PlanOut])
def list_plans(session: DbSession, region: str | None = None):
    stmt = select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.price_cents)

    if region:
        stmt = (
            stmt.join(PlanRegion, PlanRegion.plan_id == Plan.id)
            .join(Region, Region.id == PlanRegion.region_id)
            .where(
                Region.slug == region,
                Region.is_active.is_(True),
                PlanRegion.is_available.is_(True),
            )
        )

    return session.execute(stmt).scalars().all()


@router.get("/regions", response_model=list[RegionOut])
def list_regions(session: DbSession):
    return (
        session.execute(select(Region).where(Region.is_active.is_(True)).order_by(Region.name))
        .scalars()
        .all()
    )


@router.get("/images", response_model=list[ImageOut])
def list_images(session: DbSession):
    return (
        session.execute(
            select(OsImage)
            .where(OsImage.is_active.is_(True))
            .order_by(OsImage.sort_order, OsImage.name)
        )
        .scalars()
        .all()
    )


@router.get("/ssh-keys", response_model=list[SshKeyOut])
def list_ssh_keys(session: DbSession, account: CurrentAccount):
    return (
        session.execute(
            select(SshKey).where(SshKey.account_id == account.id).order_by(SshKey.created_at)
        )
        .scalars()
        .all()
    )


@router.post("/ssh-keys", response_model=SshKeyOut, status_code=status.HTTP_201_CREATED)
def add_ssh_key(
    payload: SshKeyCreate, session: DbSession, user: CurrentUser, account: CurrentAccount
):
    try:
        fingerprint = ssh_key_fingerprint(payload.public_key)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    duplicate = session.execute(
        select(SshKey).where(SshKey.account_id == account.id, SshKey.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"That key is already registered as '{duplicate.name}'")

    key = SshKey(
        account_id=account.id,
        name=payload.name.strip()[:100],
        public_key=payload.public_key.strip(),
        fingerprint=fingerprint,
    )
    session.add(key)
    session.flush()

    audit.record(
        session,
        action="ssh_key.added",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="ssh_key",
        target_id=key.id,
        fingerprint=fingerprint,
    )
    return key


@router.delete("/ssh-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ssh_key(key_id: str, session: DbSession, user: CurrentUser, account: CurrentAccount):
    key = session.get(SshKey, key_id)
    if key is None or key.account_id != account.id:
        raise NotFoundError("SSH key not found")

    session.delete(key)
    audit.record(
        session,
        action="ssh_key.deleted",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="ssh_key",
        target_id=key_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
