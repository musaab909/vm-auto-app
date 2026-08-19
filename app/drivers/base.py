"""The hypervisor driver contract.

Drivers are deliberately dumb: they take a fully-resolved spec and talk to one
hypervisor. They never touch the database, never allocate IPs, and never decide
policy — that all lives in `app.services`. This is what makes `MockDriver` a
faithful stand-in for `VSphereDriver` in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class PowerState(StrEnum):
    ON = "on"
    OFF = "off"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class NetworkSpec:
    """Guest networking, fully resolved by the caller."""

    ip_address: str
    prefix_length: int
    gateway: str | None
    dns_servers: list[str] = field(default_factory=list)
    network_name: str | None = None
    vlan_id: int | None = None


@dataclass(slots=True)
class InstanceSpec:
    """Everything a driver needs to create one VM."""

    name: str
    hostname: str
    vcpu: int
    ram_mb: int
    disk_gb: int
    template: str
    network: NetworkSpec
    # Placement (vSphere inventory names; ignored by drivers that do not use them)
    datacenter: str | None = None
    cluster: str | None = None
    resource_pool: str | None = None
    datastore: str | None = None
    folder: str | None = None
    # Guest access
    root_password: str | None = None
    ssh_keys: list[str] = field(default_factory=list)
    default_username: str = "root"
    # Correlation back to the control plane, written as VM metadata where possible
    instance_id: str | None = None
    account_id: str | None = None


@dataclass(slots=True)
class DriverInstance:
    """A driver's view of one VM."""

    ref: str
    power_state: PowerState
    uuid: str | None = None
    name: str | None = None
    ip_addresses: list[str] = field(default_factory=list)
    vcpu: int | None = None
    ram_mb: int | None = None
    disk_gb: int | None = None
    exists: bool = True


@dataclass(slots=True)
class ConsoleSession:
    ticket: str
    host: str | None = None
    port: int | None = None
    url: str | None = None


@runtime_checkable
class HypervisorDriver(Protocol):
    """Implemented by `MockDriver` and `VSphereDriver`.

    Every method must be idempotent where the operation allows it: workers retry,
    and a retry must not create a second VM or fail because the VM is already in
    the requested state.
    """

    name: str

    def create_instance(self, spec: InstanceSpec) -> DriverInstance: ...

    def get_instance(self, ref: str) -> DriverInstance: ...

    def power_on(self, ref: str) -> None: ...

    def power_off(self, ref: str, *, hard: bool = False) -> None: ...

    def reboot(self, ref: str, *, hard: bool = False) -> None: ...

    def delete_instance(self, ref: str) -> None: ...

    def resize(self, ref: str, *, vcpu: int, ram_mb: int, disk_gb: int) -> None: ...

    def rebuild(self, ref: str, spec: InstanceSpec) -> DriverInstance: ...

    def get_console(self, ref: str) -> ConsoleSession: ...

    def find_by_instance_id(self, instance_id: str) -> DriverInstance | None:
        """Locate a VM by control-plane instance id.

        Used to recover from a crash between "VM created" and "ref written to the
        database", which would otherwise orphan a running VM nobody is billing.
        """
        ...
