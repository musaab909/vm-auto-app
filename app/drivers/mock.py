"""In-memory hypervisor driver for development, CI and demos.

This is not a no-op stub like the prototype's `vmware_lib.deploy_vm()`. It keeps
real per-VM state, enforces the same legal state transitions the real hypervisor
does, and can be told to fail on demand — so the retry, reconciliation and error
paths are exercised for real in tests rather than being dead code until the first
production incident.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from app.drivers.base import (
    ConsoleSession,
    DriverInstance,
    InstanceSpec,
    NetworkSpec,
    PowerState,
)
from app.services.errors import DriverError


@dataclass
class _MockVm:
    ref: str
    uuid: str
    name: str
    vcpu: int
    ram_mb: int
    disk_gb: int
    power_state: PowerState
    network: NetworkSpec
    instance_id: str | None = None
    account_id: str | None = None
    template: str = ""
    deleted: bool = False
    history: list[str] = field(default_factory=list)


class MockDriver:
    """A driver backed by a process-local dict.

    State is shared across instances of this class (`_vms` is a class attribute)
    so the API process and an eager Celery task in the same process observe the
    same VMs. A real deployment uses `VSphereDriver`; this exists so the whole
    platform can run end-to-end with no hypervisor at all.
    """

    name = "mock"

    _vms: dict[str, _MockVm] = {}
    _lock = threading.RLock()

    def __init__(
        self,
        *,
        latency_seconds: float = 0.0,
        fail_create: bool = False,
        fail_permanently: bool = False,
    ):
        self.latency_seconds = latency_seconds
        self.fail_create = fail_create
        self.fail_permanently = fail_permanently

    # --- test helpers -------------------------------------------------------

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._vms.clear()

    @classmethod
    def all_vms(cls) -> list[_MockVm]:
        with cls._lock:
            return list(cls._vms.values())

    @classmethod
    def force_power_state(cls, ref: str, state: PowerState) -> None:
        """Change a VM out from under the control plane, to test the reconciler."""
        with cls._lock:
            vm = cls._vms.get(ref)
            if vm is None:
                raise KeyError(ref)
            vm.power_state = state
            vm.history.append(f"forced:{state}")

    @classmethod
    def force_delete(cls, ref: str) -> None:
        """Delete a VM behind the control plane's back."""
        with cls._lock:
            cls._vms.pop(ref, None)

    # --- internals ----------------------------------------------------------

    def _sleep(self) -> None:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)

    def _get(self, ref: str) -> _MockVm:
        with self._lock:
            vm = self._vms.get(ref)
        if vm is None or vm.deleted:
            raise DriverError(f"VM {ref} does not exist", permanent=True)
        return vm

    # --- driver contract ----------------------------------------------------

    def create_instance(self, spec: InstanceSpec) -> DriverInstance:
        self._sleep()
        if self.fail_create:
            raise DriverError("Simulated provisioning failure", permanent=self.fail_permanently)

        # Idempotency: a retried create must not produce a second VM.
        if spec.instance_id:
            existing = self.find_by_instance_id(spec.instance_id)
            if existing is not None:
                return existing

        ref = f"vm-{uuid.uuid4().hex[:10]}"
        vm = _MockVm(
            ref=ref,
            uuid=str(uuid.uuid4()),
            name=spec.name,
            vcpu=spec.vcpu,
            ram_mb=spec.ram_mb,
            disk_gb=spec.disk_gb,
            power_state=PowerState.ON,
            network=spec.network,
            instance_id=spec.instance_id,
            account_id=spec.account_id,
            template=spec.template,
        )
        vm.history.append("created")
        with self._lock:
            self._vms[ref] = vm
        return self._to_driver_instance(vm)

    def get_instance(self, ref: str) -> DriverInstance:
        with self._lock:
            vm = self._vms.get(ref)
        if vm is None or vm.deleted:
            return DriverInstance(ref=ref, power_state=PowerState.UNKNOWN, exists=False)
        return self._to_driver_instance(vm)

    def power_on(self, ref: str) -> None:
        self._sleep()
        vm = self._get(ref)
        # Idempotent: already-on is success, not an error, because workers retry.
        if vm.power_state is not PowerState.ON:
            vm.power_state = PowerState.ON
            vm.history.append("power_on")

    def power_off(self, ref: str, *, hard: bool = False) -> None:
        self._sleep()
        vm = self._get(ref)
        if vm.power_state is not PowerState.OFF:
            vm.power_state = PowerState.OFF
            vm.history.append("power_off:hard" if hard else "power_off:graceful")

    def reboot(self, ref: str, *, hard: bool = False) -> None:
        self._sleep()
        vm = self._get(ref)
        if vm.power_state is not PowerState.ON:
            raise DriverError("Cannot reboot a VM that is not powered on", permanent=True)
        vm.history.append("reboot:hard" if hard else "reboot:graceful")

    def delete_instance(self, ref: str) -> None:
        self._sleep()
        with self._lock:
            vm = self._vms.get(ref)
            # Deleting an already-absent VM is success — the desired end state holds.
            if vm is None:
                return
            del self._vms[ref]

    def resize(self, ref: str, *, vcpu: int, ram_mb: int, disk_gb: int) -> None:
        self._sleep()
        vm = self._get(ref)
        if vm.power_state is not PowerState.OFF:
            raise DriverError("VM must be powered off before resizing", permanent=True)
        if disk_gb < vm.disk_gb:
            raise DriverError("Disks cannot be shrunk", permanent=True)
        vm.vcpu, vm.ram_mb, vm.disk_gb = vcpu, ram_mb, disk_gb
        vm.history.append(f"resize:{vcpu}/{ram_mb}/{disk_gb}")

    def rebuild(self, ref: str, spec: InstanceSpec) -> DriverInstance:
        self._sleep()
        vm = self._get(ref)
        vm.template = spec.template
        vm.power_state = PowerState.ON
        vm.history.append(f"rebuild:{spec.template}")
        return self._to_driver_instance(vm)

    def get_console(self, ref: str) -> ConsoleSession:
        vm = self._get(ref)
        return ConsoleSession(
            ticket=f"mock-ticket-{uuid.uuid4().hex}",
            host="mock-console.local",
            port=443,
            url=f"wss://mock-console.local/ticket/{vm.ref}",
        )

    def find_by_instance_id(self, instance_id: str) -> DriverInstance | None:
        with self._lock:
            for vm in self._vms.values():
                if vm.instance_id == instance_id and not vm.deleted:
                    return self._to_driver_instance(vm)
        return None

    def _to_driver_instance(self, vm: _MockVm) -> DriverInstance:
        return DriverInstance(
            ref=vm.ref,
            power_state=vm.power_state,
            uuid=vm.uuid,
            name=vm.name,
            ip_addresses=[vm.network.ip_address] if vm.network.ip_address else [],
            vcpu=vm.vcpu,
            ram_mb=vm.ram_mb,
            disk_gb=vm.disk_gb,
            exists=True,
        )
