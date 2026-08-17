"""VMware vSphere driver (pyvmomi).

Guest configuration is injected with cloud-init's NoCloud datasource over
`guestinfo`, not with VMware customization specs. Customization specs are the
legacy path: they need Perl/open-vm-tools inside the guest, they serialise badly
across concurrent clones, and they cannot express arbitrary user-data. The
guestinfo route is what modern Linux templates expect.

The template must be prepared with cloud-init and open-vm-tools installed and the
NoCloud/VMware datasource enabled. See `docs/vcenter-setup.md`.

TLS verification is on by default and there is no code path that disables it
implicitly; a private CA is supplied through `vsphere_ca_bundle`.
"""

from __future__ import annotations

import base64
import ssl
import threading
import time
from typing import Any

from app.config import get_settings
from app.drivers.base import (
    ConsoleSession,
    DriverInstance,
    InstanceSpec,
    PowerState,
)
from app.services.errors import DriverError

try:  # pragma: no cover - exercised only where pyvmomi is installed
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim, vmodl
except ImportError:  # pragma: no cover
    # Kept importable without pyvmomi so a mock-driver deployment does not need it.
    # `VSphereDriver.__init__` raises a clear DriverError if it is actually used.
    SmartConnect = Disconnect = None
    vim = vmodl = None


# Written into the VM's extraConfig so a VM can always be traced back to the
# control-plane row that owns it, and so orphans are identifiable after a crash.
GUEST_KEY_INSTANCE_ID = "guestinfo.hostplane.instance_id"
GUEST_KEY_ACCOUNT_ID = "guestinfo.hostplane.account_id"

_POWER_STATE_MAP = {
    "poweredOn": PowerState.ON,
    "poweredOff": PowerState.OFF,
    "suspended": PowerState.SUSPENDED,
}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def build_network_config(spec: InstanceSpec) -> str:
    """cloud-init network-config v2 for a single static-addressed NIC."""
    net = spec.network
    lines = [
        "version: 2",
        "ethernets:",
        "  id0:",
        "    match:",
        "      name: e*",
        "    dhcp4: false",
        "    dhcp6: false",
        "    addresses:",
        f"      - {net.ip_address}/{net.prefix_length}",
    ]
    if net.gateway:
        # `routes:` rather than the deprecated `gateway4:`, which newer netplan
        # versions warn on and will eventually drop.
        lines += [
            "    routes:",
            "      - to: default",
            f"        via: {net.gateway}",
        ]
    if net.dns_servers:
        lines.append("    nameservers:")
        lines.append("      addresses:")
        lines.extend(f"        - {server}" for server in net.dns_servers)
    return "\n".join(lines) + "\n"


def build_metadata(spec: InstanceSpec) -> str:
    return (
        "\n".join(
            [
                f"instance-id: {spec.instance_id or spec.name}",
                f"local-hostname: {spec.hostname}",
            ]
        )
        + "\n"
    )


def build_userdata(spec: InstanceSpec) -> str:
    """cloud-config setting the hostname, root password and SSH keys."""
    lines = [
        "#cloud-config",
        f"hostname: {spec.hostname}",
        f"fqdn: {spec.hostname}",
        "preserve_hostname: false",
        "manage_etc_hosts: true",
    ]

    if spec.root_password:
        lines += [
            "chpasswd:",
            "  expire: false",
            "  list:",
            f"    - {spec.default_username}:{spec.root_password}",
            # Password login is only enabled because a freshly ordered VPS with no
            # SSH key would otherwise be unreachable. Customers who registered a
            # key get key-only access.
            f"ssh_pwauth: {str(not spec.ssh_keys).lower()}",
        ]

    if spec.ssh_keys:
        if spec.default_username == "root":
            lines.append("disable_root: false")
            lines.append("ssh_authorized_keys:")
            lines.extend(f"  - {key}" for key in spec.ssh_keys)
        else:
            lines += [
                "users:",
                "  - default",
                f"  - name: {spec.default_username}",
                "    sudo: ALL=(ALL) NOPASSWD:ALL",
                "    shell: /bin/bash",
                "    ssh_authorized_keys:",
            ]
            lines.extend(f"      - {key}" for key in spec.ssh_keys)

    lines += ["runcmd:", "  - [ systemctl, restart, sshd ]"]
    return "\n".join(lines) + "\n"


class VSphereDriver:
    """Talks to one vCenter.

    A connection is opened lazily and reused; pyvmomi sessions are not thread-safe
    for concurrent mutation, so access is serialised behind a lock. Workers are
    separate processes, so this does not become a global bottleneck.
    """

    name = "vsphere"

    def __init__(
        self,
        *,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        port: int | None = None,
        verify_ssl: bool | None = None,
        ca_bundle: str | None = None,
        task_timeout: int | None = None,
    ):
        settings = get_settings()
        self.host = host or settings.vsphere_host
        self.user = user or settings.vsphere_user
        self.password = password or settings.vsphere_password
        self.port = port or settings.vsphere_port
        self.verify_ssl = settings.vsphere_verify_ssl if verify_ssl is None else verify_ssl
        self.ca_bundle = ca_bundle or settings.vsphere_ca_bundle
        self.task_timeout = task_timeout or settings.vsphere_task_timeout_seconds

        if not all([self.host, self.user, self.password]):
            raise DriverError(
                "vSphere driver requires HOSTPLANE_VSPHERE_HOST, _USER and _PASSWORD",
                permanent=True,
            )
        if SmartConnect is None:
            raise DriverError("pyvmomi is not installed", permanent=True)

        self._si: Any = None
        self._lock = threading.RLock()

    # --- connection ---------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        if not self.verify_ssl:
            # Reachable only when an operator explicitly opts out in a non-production
            # environment; `Settings.check_production_safety()` rejects it otherwise.
            return ssl._create_unverified_context()
        return ssl.create_default_context(cafile=self.ca_bundle)

    def connect(self) -> Any:
        with self._lock:
            if self._si is not None:
                try:
                    self._si.CurrentTime()
                    return self._si
                except Exception:
                    self._si = None
            try:
                self._si = SmartConnect(
                    host=self.host,
                    user=self.user,
                    pwd=self.password,
                    port=self.port,
                    sslContext=self._ssl_context(),
                )
            except ssl.SSLError as exc:
                raise DriverError(
                    f"TLS verification failed against vCenter {self.host}: {exc}. "
                    "Point HOSTPLANE_VSPHERE_CA_BUNDLE at the CA that signed it.",
                    permanent=True,
                ) from exc
            except Exception as exc:
                raise DriverError(f"Cannot connect to vCenter {self.host}: {exc}") from exc
            return self._si

    def close(self) -> None:
        with self._lock:
            if self._si is not None:
                try:
                    Disconnect(self._si)
                finally:
                    self._si = None

    @property
    def content(self) -> Any:
        return self.connect().RetrieveContent()

    # --- inventory lookup ---------------------------------------------------

    def _find_object(self, vim_type: Any, name: str, root: Any = None) -> Any:
        content = self.content
        container = content.viewManager.CreateContainerView(
            root or content.rootFolder, [vim_type], True
        )
        try:
            for obj in container.view:
                if obj.name == name:
                    return obj
        finally:
            container.Destroy()
        return None

    def _require(self, vim_type: Any, name: str, label: str, root: Any = None) -> Any:
        obj = self._find_object(vim_type, name, root)
        if obj is None:
            raise DriverError(f"{label} '{name}' not found in vCenter inventory", permanent=True)
        return obj

    def _find_vm_by_ref(self, ref: str) -> Any:
        """Resolve a MoRef string such as "vm-1234" back to a managed object."""
        content = self.content
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            for vm in container.view:
                if vm._moId == ref:
                    return vm
        finally:
            container.Destroy()
        return None

    def _require_vm(self, ref: str) -> Any:
        vm = self._find_vm_by_ref(ref)
        if vm is None:
            raise DriverError(f"VM {ref} not found in vCenter", permanent=True)
        return vm

    # --- task handling ------------------------------------------------------

    def _wait_for_task(self, task: Any, *, what: str) -> Any:
        """Block until a vCenter task settles, with an explicit timeout.

        pyvmomi offers no timeout of its own; without this a wedged task would pin
        a worker forever.
        """
        deadline = time.monotonic() + self.task_timeout
        while True:
            state = task.info.state
            if state == vim.TaskInfo.State.success:
                return task.info.result
            if state == vim.TaskInfo.State.error:
                fault = task.info.error
                message = getattr(fault, "msg", str(fault))
                # An invalid spec or a missing object will fail identically on every
                # retry, so mark those permanent and stop burning attempts.
                permanent = (
                    isinstance(
                        fault,
                        (
                            vim.fault.InvalidArgument,
                            vim.fault.NotFound,
                            vim.fault.DuplicateName,
                            vim.fault.InvalidState,
                            vim.fault.InsufficientResourcesFault,
                        ),
                    )
                    if vim is not None
                    else False
                )
                raise DriverError(f"{what} failed: {message}", permanent=permanent)
            if time.monotonic() > deadline:
                raise DriverError(f"{what} timed out after {self.task_timeout}s")
            time.sleep(2)

    # --- spec construction --------------------------------------------------

    def _guest_extra_config(self, spec: InstanceSpec) -> list[Any]:
        """cloud-init NoCloud payload plus control-plane correlation keys."""
        options = [
            vim.option.OptionValue(key="guestinfo.metadata", value=_b64(build_metadata(spec))),
            vim.option.OptionValue(key="guestinfo.metadata.encoding", value="base64"),
            vim.option.OptionValue(key="guestinfo.userdata", value=_b64(build_userdata(spec))),
            vim.option.OptionValue(key="guestinfo.userdata.encoding", value="base64"),
            vim.option.OptionValue(
                key="guestinfo.vendordata", value=_b64(build_network_config(spec))
            ),
            vim.option.OptionValue(key="guestinfo.vendordata.encoding", value="base64"),
        ]
        if spec.instance_id:
            options.append(
                vim.option.OptionValue(key=GUEST_KEY_INSTANCE_ID, value=spec.instance_id)
            )
        if spec.account_id:
            options.append(vim.option.OptionValue(key=GUEST_KEY_ACCOUNT_ID, value=spec.account_id))
        return options

    def _nic_change_spec(self, template: Any, network_name: str | None) -> list[Any]:
        """Re-point the template's first NIC at the target port group."""
        if not network_name:
            return []

        nic = next(
            (
                dev
                for dev in template.config.hardware.device
                if isinstance(dev, vim.vm.device.VirtualEthernetCard)
            ),
            None,
        )
        if nic is None:
            raise DriverError(
                f"Template '{template.name}' has no network adapter to configure", permanent=True
            )

        network = self._find_object(vim.Network, network_name)
        if network is None:
            raise DriverError(f"Network '{network_name}' not found", permanent=True)

        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        nic_spec.device = nic

        backing: Any
        if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
            # Distributed port groups are addressed by switch UUID + port key, not
            # by name like a standard vSwitch port group.
            switch = network.config.distributedVirtualSwitch
            if switch is None:
                raise DriverError(
                    f"Distributed port group '{network_name}' has no backing switch",
                    permanent=True,
                )
            port = vim.dvs.PortConnection()
            port.portgroupKey = network.key
            port.switchUuid = switch.uuid
            backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
            backing.port = port
        else:
            backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
            backing.network = network
            backing.deviceName = network_name

        nic_spec.device.backing = backing
        nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
            startConnected=True, allowGuestControl=True, connected=True
        )
        return [nic_spec]

    def _disk_change_spec(self, template: Any, disk_gb: int) -> list[Any]:
        """Grow the template's primary disk to the plan size."""
        disk = next(
            (
                dev
                for dev in template.config.hardware.device
                if isinstance(dev, vim.vm.device.VirtualDisk)
            ),
            None,
        )
        if disk is None:
            raise DriverError(f"Template '{template.name}' has no virtual disk", permanent=True)

        target_kb = disk_gb * 1024 * 1024
        if target_kb < disk.capacityInKB:
            raise DriverError(
                f"Plan disk {disk_gb}GB is smaller than template disk "
                f"{disk.capacityInKB // (1024 * 1024)}GB; disks cannot be shrunk",
                permanent=True,
            )
        if target_kb == disk.capacityInKB:
            return []

        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        disk_spec.device = disk
        disk_spec.device.capacityInKB = target_kb
        return [disk_spec]

    # --- driver contract ----------------------------------------------------

    def create_instance(self, spec: InstanceSpec) -> DriverInstance:
        # A retry after a partial failure must adopt the existing VM, never clone
        # a second one.
        if spec.instance_id:
            existing = self.find_by_instance_id(spec.instance_id)
            if existing is not None:
                return existing

        template = self._require(vim.VirtualMachine, spec.template, "Template")

        datacenter = (
            self._require(vim.Datacenter, spec.datacenter, "Datacenter")
            if spec.datacenter
            else self.content.rootFolder.childEntity[0]
        )

        folder = (
            self._require(vim.Folder, spec.folder, "Folder", root=datacenter)
            if spec.folder
            else datacenter.vmFolder
        )

        relocate = vim.vm.RelocateSpec()
        if spec.datastore:
            relocate.datastore = self._require(vim.Datastore, spec.datastore, "Datastore")
        if spec.resource_pool:
            relocate.pool = self._require(vim.ResourcePool, spec.resource_pool, "Resource pool")
        elif spec.cluster:
            cluster = self._require(vim.ClusterComputeResource, spec.cluster, "Cluster")
            relocate.pool = cluster.resourcePool
        else:
            raise DriverError(
                "Region must define either a vSphere cluster or a resource pool", permanent=True
            )

        config = vim.vm.ConfigSpec()
        config.numCPUs = spec.vcpu
        config.memoryMB = spec.ram_mb
        config.extraConfig = self._guest_extra_config(spec)
        config.deviceChange = self._nic_change_spec(
            template, spec.network.network_name
        ) + self._disk_change_spec(template, spec.disk_gb)

        clone_spec = vim.vm.CloneSpec()
        clone_spec.location = relocate
        clone_spec.config = config
        # Powered on only after cloud-init data is in extraConfig, so the guest
        # sees its configuration on first boot.
        clone_spec.powerOn = True
        clone_spec.template = False

        try:
            task = template.Clone(folder=folder, name=spec.name, spec=clone_spec)
        except vmodl.MethodFault as exc:
            raise DriverError(
                f"Clone request rejected: {getattr(exc, 'msg', exc)}", permanent=True
            ) from exc

        vm = self._wait_for_task(task, what=f"Clone of '{spec.template}' to '{spec.name}'")
        return self._to_driver_instance(vm)

    def get_instance(self, ref: str) -> DriverInstance:
        vm = self._find_vm_by_ref(ref)
        if vm is None:
            return DriverInstance(ref=ref, power_state=PowerState.UNKNOWN, exists=False)
        return self._to_driver_instance(vm)

    def power_on(self, ref: str) -> None:
        vm = self._require_vm(ref)
        if vm.runtime.powerState == "poweredOn":
            return
        self._wait_for_task(vm.PowerOn(), what=f"Power on {ref}")

    def power_off(self, ref: str, *, hard: bool = False) -> None:
        vm = self._require_vm(ref)
        if vm.runtime.powerState == "poweredOff":
            return
        if not hard and vm.guest.toolsRunning == "guestToolsRunning":
            # ShutdownGuest returns immediately and gives no task to await, so poll
            # for the state change and fall back to a hard stop.
            vm.ShutdownGuest()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if vm.runtime.powerState == "poweredOff":
                    return
                time.sleep(2)
        self._wait_for_task(vm.PowerOff(), what=f"Power off {ref}")

    def reboot(self, ref: str, *, hard: bool = False) -> None:
        vm = self._require_vm(ref)
        if vm.runtime.powerState != "poweredOn":
            raise DriverError("Cannot reboot a VM that is not powered on", permanent=True)
        if not hard and vm.guest.toolsRunning == "guestToolsRunning":
            vm.RebootGuest()
            return
        self._wait_for_task(vm.ResetVM_Task(), what=f"Reset {ref}")

    def delete_instance(self, ref: str) -> None:
        vm = self._find_vm_by_ref(ref)
        if vm is None:
            return  # Already gone; the desired end state holds.
        if vm.runtime.powerState != "poweredOff":
            self._wait_for_task(vm.PowerOff(), what=f"Power off {ref} before delete")
        self._wait_for_task(vm.Destroy_Task(), what=f"Destroy {ref}")

    def resize(self, ref: str, *, vcpu: int, ram_mb: int, disk_gb: int) -> None:
        vm = self._require_vm(ref)
        if vm.runtime.powerState != "poweredOff":
            raise DriverError("VM must be powered off before resizing", permanent=True)

        config = vim.vm.ConfigSpec()
        config.numCPUs = vcpu
        config.memoryMB = ram_mb
        disk_changes = self._disk_change_spec(vm, disk_gb)
        if disk_changes:
            config.deviceChange = disk_changes
        self._wait_for_task(vm.ReconfigVM_Task(spec=config), what=f"Resize {ref}")

    def rebuild(self, ref: str, spec: InstanceSpec) -> DriverInstance:
        """Reinstall by destroying and re-cloning.

        vSphere has no in-place reimage, and this is destructive by design — the
        caller confirms with the customer first.
        """
        self.delete_instance(ref)
        return self.create_instance(spec)

    def get_console(self, ref: str) -> ConsoleSession:
        vm = self._require_vm(ref)
        try:
            ticket = vm.AcquireTicket("webmks")
        except vmodl.MethodFault as exc:
            raise DriverError(
                f"Could not acquire console ticket: {getattr(exc, 'msg', exc)}"
            ) from exc
        return ConsoleSession(
            ticket=ticket.ticket,
            host=ticket.host,
            port=ticket.port,
            url=f"wss://{ticket.host}:{ticket.port}/ticket/{ticket.ticket}",
        )

    def find_by_instance_id(self, instance_id: str) -> DriverInstance | None:
        content = self.content
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            for vm in container.view:
                config = getattr(vm, "config", None)
                if config is None:
                    continue
                for option in getattr(config, "extraConfig", []) or []:
                    if option.key == GUEST_KEY_INSTANCE_ID and option.value == instance_id:
                        return self._to_driver_instance(vm)
        finally:
            container.Destroy()
        return None

    # --- mapping ------------------------------------------------------------

    def _to_driver_instance(self, vm: Any) -> DriverInstance:
        config = getattr(vm, "config", None)
        hardware = getattr(config, "hardware", None) if config else None
        guest = getattr(vm, "guest", None)

        ips: list[str] = []
        if guest is not None:
            for nic in getattr(guest, "net", []) or []:
                ips.extend(getattr(nic, "ipAddress", []) or [])

        disk_gb = None
        if hardware is not None:
            total_kb = sum(
                dev.capacityInKB
                for dev in hardware.device
                if isinstance(dev, vim.vm.device.VirtualDisk)
            )
            disk_gb = total_kb // (1024 * 1024) if total_kb else None

        return DriverInstance(
            ref=vm._moId,
            power_state=_POWER_STATE_MAP.get(vm.runtime.powerState, PowerState.UNKNOWN),
            uuid=getattr(config, "uuid", None) if config else None,
            name=vm.name,
            ip_addresses=ips,
            vcpu=getattr(hardware, "numCPU", None) if hardware else None,
            ram_mb=getattr(hardware, "memoryMB", None) if hardware else None,
            disk_gb=disk_gb,
            exists=True,
        )
