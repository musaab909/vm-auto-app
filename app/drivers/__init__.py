"""Hypervisor drivers and their factory."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.drivers.base import (
    ConsoleSession,
    DriverInstance,
    HypervisorDriver,
    InstanceSpec,
    NetworkSpec,
    PowerState,
)
from app.drivers.mock import MockDriver

__all__ = [
    "ConsoleSession",
    "DriverInstance",
    "HypervisorDriver",
    "InstanceSpec",
    "MockDriver",
    "NetworkSpec",
    "PowerState",
    "get_driver",
    "reset_driver_cache",
]


@lru_cache(maxsize=1)
def get_driver() -> HypervisorDriver:
    """Return the configured driver.

    `VSphereDriver` is imported lazily so that a mock-driver deployment never
    needs pyvmomi to import cleanly, and so a vCenter misconfiguration surfaces
    when the driver is first used rather than at module import.
    """
    settings = get_settings()
    if settings.driver == "vsphere":
        from app.drivers.vsphere import VSphereDriver

        return VSphereDriver()
    return MockDriver()


def reset_driver_cache() -> None:
    get_driver.cache_clear()
