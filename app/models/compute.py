"""Instances and the async job records that drive them."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EnumString, Timestamped, UUIDPrimaryKey
from app.models.enums import InstanceStatus, JobStatus, JobType

if TYPE_CHECKING:
    from app.models.catalog import OsImage, Plan, Region
    from app.models.identity import Account
    from app.models.network import IpAddress


class Instance(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "instances"
    __table_args__ = (
        Index("ix_instances_account_status", "account_id", "status"),
        Index("ix_instances_hypervisor_ref", "hypervisor_ref"),
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    image_id: Mapped[str] = mapped_column(
        ForeignKey("os_images.id", ondelete="RESTRICT"), nullable=False
    )
    region_id: Mapped[str] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )

    label: Mapped[str] = mapped_column(String(100), nullable=False)
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    status: Mapped[InstanceStatus] = mapped_column(
        EnumString(InstanceStatus, 20),
        default=InstanceStatus.PENDING,
        nullable=False,
        index=True,
    )

    # Effective resources. Copied from the plan at order time so a later plan edit
    # cannot silently change what an existing customer is running.
    vcpu: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False)

    # Opaque hypervisor handle (a vSphere MoRef such as "vm-1234").
    hypervisor_ref: Mapped[str | None] = mapped_column(String(120))
    hypervisor_uuid: Mapped[str | None] = mapped_column(String(64))

    # Only a hash of the generated root password is kept. The plaintext is shown
    # once at provisioning time and never persisted.
    root_password_hash: Mapped[str | None] = mapped_column(String(255))
    root_password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    default_username: Mapped[str] = mapped_column(String(40), default="root", nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set when the reconciler finds DB state disagreeing with the hypervisor.
    drift_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(back_populates="instances")
    plan: Mapped[Plan] = relationship()
    image: Mapped[OsImage] = relationship()
    region: Mapped[Region] = relationship()
    ip_addresses: Mapped[list[IpAddress]] = relationship(
        foreign_keys="IpAddress.instance_id", viewonly=True
    )
    jobs: Mapped[list[Job]] = relationship(
        back_populates="instance", cascade="all, delete-orphan", order_by="Job.created_at.desc()"
    )

    @property
    def primary_ip(self) -> str | None:
        for addr in self.ip_addresses:
            if addr.is_primary:
                return addr.address
        return None

    @property
    def is_actionable(self) -> bool:
        """True when the customer may issue a power action right now."""
        return self.status in {
            InstanceStatus.RUNNING,
            InstanceStatus.STOPPED,
            InstanceStatus.ERROR,
        }


class Job(UUIDPrimaryKey, Timestamped, Base):
    """A user-visible async operation.

    Every hypervisor call runs through one of these, so the portal can show real
    progress and failures instead of a spinner that never resolves.
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_instance_status", "instance_id", "status"),)

    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    type: Mapped[JobType] = mapped_column(EnumString(JobType, 30), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        EnumString(JobStatus, 20), default=JobStatus.QUEUED, nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    instance: Mapped[Instance | None] = relationship(back_populates="jobs")

    @property
    def is_finished(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}


class ConsoleTicket(UUIDPrimaryKey, Base):
    """Short-lived console session handle.

    The WebMKS ticket itself is a bearer credential for the VM's console, so it is
    stored with an explicit expiry and single-use flag rather than being embedded
    in a page that might be cached or shared.
    """

    __tablename__ = "console_tickets"

    instance_id: Mapped[str] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issued_to_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ticket: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str | None] = mapped_column(String(200))
    port: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    single_use: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
