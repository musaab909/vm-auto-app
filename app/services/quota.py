"""Per-account resource quotas."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.catalog import Plan
from app.models.compute import Instance
from app.models.enums import InstanceStatus
from app.models.identity import Account, Quota
from app.services.errors import QuotaExceededError

# Instances in these states no longer consume quota.
_RELEASED_STATES = (InstanceStatus.DELETED,)


def get_or_create_quota(session: Session, account: Account) -> Quota:
    if account.quota is not None:
        return account.quota
    settings = get_settings()
    quota = Quota(
        account_id=account.id,
        max_instances=settings.default_max_instances,
        max_ips=settings.default_max_ips,
    )
    session.add(quota)
    session.flush()
    account.quota = quota
    return quota


def current_usage(session: Session, account_id: str) -> dict[str, int]:
    row = session.execute(
        select(
            func.count(Instance.id),
            func.coalesce(func.sum(Instance.vcpu), 0),
            func.coalesce(func.sum(Instance.ram_mb), 0),
            func.coalesce(func.sum(Instance.disk_gb), 0),
        ).where(
            Instance.account_id == account_id,
            Instance.status.not_in(_RELEASED_STATES),
        )
    ).one()
    return {
        "instances": int(row[0]),
        "vcpu": int(row[1]),
        "ram_mb": int(row[2]),
        "disk_gb": int(row[3]),
    }


def check_can_create_instance(session: Session, account: Account, plan: Plan) -> None:
    """Raise `QuotaExceededError` if adding `plan` would breach the account's quota."""
    quota = get_or_create_quota(session, account)
    usage = current_usage(session, account.id)

    checks = (
        ("instances", usage["instances"] + 1, quota.max_instances, "instances"),
        ("vcpu", usage["vcpu"] + plan.vcpu, quota.max_vcpu, "vCPUs"),
        ("ram_mb", usage["ram_mb"] + plan.ram_mb, quota.max_ram_mb, "MB of RAM"),
        ("disk_gb", usage["disk_gb"] + plan.disk_gb, quota.max_disk_gb, "GB of disk"),
    )

    for key, proposed, limit, label in checks:
        if proposed > limit:
            raise QuotaExceededError(
                f"This would use {proposed} {label}, above your limit of {limit}. "
                "Contact support to raise your quota.",
                details={"resource": key, "requested": proposed, "limit": limit},
            )
