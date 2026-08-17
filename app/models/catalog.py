"""Product catalog: regions, plans and OS images."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EnumString, Timestamped, UUIDPrimaryKey
from app.models.enums import BillingPeriod


class Region(UUIDPrimaryKey, Timestamped, Base):
    """A placement target, mapped onto concrete vSphere inventory objects.

    Names here are the vCenter inventory names the driver resolves at runtime; the
    control plane deliberately stores no MoRefs for them, since they change when
    inventory is recreated.
    """

    __tablename__ = "regions"

    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # vSphere placement
    vsphere_datacenter: Mapped[str | None] = mapped_column(String(200))
    vsphere_cluster: Mapped[str | None] = mapped_column(String(200))
    vsphere_resource_pool: Mapped[str | None] = mapped_column(String(200))
    vsphere_datastore: Mapped[str | None] = mapped_column(String(200))
    vsphere_folder: Mapped[str | None] = mapped_column(String(400))
    vsphere_network: Mapped[str | None] = mapped_column(String(200))

    plans: Mapped[list[PlanRegion]] = relationship(
        back_populates="region", cascade="all, delete-orphan"
    )


class Plan(UUIDPrimaryKey, Timestamped, Base):
    """A fixed VPS plan. Contabo-style: no metering, one monthly price."""

    __tablename__ = "plans"

    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    vcpu: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    bandwidth_tb: Mapped[int] = mapped_column(Integer, default=32, nullable=False)

    # Money is integer minor units with an explicit currency. No floats, ever.
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    billing_period: Mapped[BillingPeriod] = mapped_column(
        EnumString(BillingPeriod, 20), default=BillingPeriod.MONTHLY, nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    regions: Mapped[list[PlanRegion]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    @property
    def price_display(self) -> str:
        return f"{self.price_cents / 100:.2f} {self.currency}"


class PlanRegion(UUIDPrimaryKey, Base):
    """Availability of a plan in a region."""

    __tablename__ = "plan_regions"
    __table_args__ = (UniqueConstraint("plan_id", "region_id", name="uq_plan_regions_pair"),)

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    region_id: Mapped[str] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="regions")
    region: Mapped[Region] = relationship(back_populates="plans")


class OsImage(UUIDPrimaryKey, Timestamped, Base):
    """A bootable OS template.

    `vsphere_template` is the inventory name of a prepared VM template with
    cloud-init and open-vm-tools installed and the NoCloud datasource enabled.
    """

    __tablename__ = "os_images"

    slug: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    family: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    vsphere_template: Mapped[str | None] = mapped_column(String(200))
    min_disk_gb: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    default_username: Mapped[str] = mapped_column(String(40), default="root", nullable=False)
    supports_cloud_init: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
