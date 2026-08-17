"""IP address management.

This replaces the prototype's ping sweep. Addresses are materialised rows with an
explicit lifecycle, so allocation is a single indexed `SELECT ... FOR UPDATE SKIP
LOCKED` instead of 254 sequential pings, and two concurrent orders can never be
handed the same address.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EnumString, Timestamped, UUIDPrimaryKey
from app.models.enums import IPStatus, IPVersion


class IpPool(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "ip_pools"

    region_id: Mapped[str] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[IPVersion] = mapped_column(
        EnumString(IPVersion, 10), default=IPVersion.V4, nullable=False
    )
    gateway: Mapped[str | None] = mapped_column(String(64))
    prefix_length: Mapped[int] = mapped_column(Integer, nullable=False)
    dns_servers: Mapped[str] = mapped_column(String(200), default="1.1.1.1,8.8.8.8", nullable=False)

    # vSphere port group backing this pool. Overrides the region default.
    vsphere_network: Mapped[str | None] = mapped_column(String(200))
    vlan_id: Mapped[int | None] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    addresses: Mapped[list[IpAddress]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )

    @property
    def dns_list(self) -> list[str]:
        return [s.strip() for s in self.dns_servers.split(",") if s.strip()]


class IpAddress(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "ip_addresses"
    __table_args__ = (
        UniqueConstraint("pool_id", "address", name="uq_ip_addresses_pool_address"),
        # Partial-friendly composite index: allocation always filters on
        # (pool_id, status) and orders by address, so this covers the hot path.
        Index("ix_ip_addresses_pool_status", "pool_id", "status"),
    )

    pool_id: Mapped[str] = mapped_column(
        ForeignKey("ip_pools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IPStatus] = mapped_column(
        EnumString(IPStatus, 20), default=IPStatus.AVAILABLE, nullable=False, index=True
    )
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Addresses sit in quarantine after release so a new customer does not inherit
    # the previous tenant's traffic, DNS records or blocklist reputation.
    reusable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    notes: Mapped[str | None] = mapped_column(String(300))

    pool: Mapped[IpPool] = relationship(back_populates="addresses")
