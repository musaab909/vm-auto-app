"""vCenter preflight checks.

The vSphere driver has a large surface that only fails at provisioning time: a
missing privilege, a renamed port group or an oversized template all present to
the customer as a failed order, minutes after the fact and with a vCenter fault
message for a cause.

This module front-loads those failures. Every check answers one question, and a
failure carries the specific remedy rather than just the symptom — the intent is
that an operator can run `hostplane doctor`, fix what it lists, and re-run until
it is clean, without reading the driver source.

Kept separate from `vsphere.py` so the driver stays focused on doing the work and
this stays focused on proving it can be done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import OsImage, Plan, PlanRegion, Region
from app.models.network import IpPool
from app.services import ipam
from app.services.errors import DriverError

# Privileges the driver actually exercises. Kept here rather than only in the
# docs so the check and the requirement cannot drift apart.
REQUIRED_PRIVILEGES: dict[str, str] = {
    "VirtualMachine.Provisioning.DeployTemplate": "clone a VM from a template",
    "VirtualMachine.Provisioning.Clone": "clone a VM",
    "VirtualMachine.Config.AddNewDisk": "size the disk during clone",
    "VirtualMachine.Config.DiskExtend": "grow the disk to the plan size",
    "VirtualMachine.Config.CPUCount": "set the plan's vCPU count",
    "VirtualMachine.Config.Memory": "set the plan's memory",
    "VirtualMachine.Config.EditDevice": "attach the NIC to the port group",
    # The one most often missed. Without it a clone succeeds and the guestinfo
    # keys silently do not land, so the guest boots with no network and no
    # credentials — and nothing in the vCenter log says why.
    "VirtualMachine.Config.AdvancedConfig": "write the cloud-init guestinfo keys",
    "VirtualMachine.Interact.PowerOn": "start a server",
    "VirtualMachine.Interact.PowerOff": "stop a server",
    "VirtualMachine.Interact.Reset": "hard-reboot a server",
    "VirtualMachine.Interact.ConsoleInteract": "open the web console",
    "VirtualMachine.Inventory.CreateFromExisting": "register the cloned VM",
    "VirtualMachine.Inventory.Delete": "destroy a server",
    "Resource.AssignVMToPool": "place the VM in a resource pool",
    "Datastore.AllocateSpace": "consume datastore space",
    "Datastore.Browse": "inspect the datastore",
    "Network.Assign": "attach the VM to a network",
}


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class Check:
    """One preflight result.

    `remedy` is the point of the whole module: a failing check that does not say
    what to do about it just moves the debugging session, it does not shorten it.
    """

    name: str
    status: Status
    detail: str = ""
    remedy: str = ""

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str = "", remedy: str = "") -> Check:
        check = Check(name=name, status=status, detail=detail, remedy=remedy)
        self.checks.append(check)
        return check

    def ok(self, name: str, detail: str = "") -> Check:
        return self.add(name, Status.PASS, detail)

    def fail(self, name: str, detail: str, remedy: str = "") -> Check:
        return self.add(name, Status.FAIL, detail, remedy)

    def warn(self, name: str, detail: str, remedy: str = "") -> Check:
        return self.add(name, Status.WARN, detail, remedy)

    def skip(self, name: str, detail: str = "") -> Check:
        return self.add(name, Status.SKIP, detail)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def passed(self) -> bool:
        return not self.failures

    def counts(self) -> dict[str, int]:
        result = {s.value: 0 for s in Status}
        for check in self.checks:
            result[check.status.value] += 1
        return result


def _gb(bytes_: int | None) -> str:
    if not bytes_:
        return "unknown"
    return f"{bytes_ / (1024**3):.0f} GB"


def run_preflight(session: Session, driver: object | None = None) -> Report:
    """Run every check that the current configuration allows.

    Ordered so the cheapest and most fundamental failures surface first: there is
    no value in reporting twelve missing port groups when the real problem is
    that the credentials are wrong.
    """
    report = Report()

    # Catalogue checks need no hypervisor, so they run either way. A platform
    # with no regions or no IP capacity cannot serve orders regardless of whether
    # vCenter is reachable.
    _check_catalog(session, report)

    if driver is None:
        report.skip(
            "vCenter connection",
            "driver is 'mock' — no vCenter checks run. Set HOSTPLANE_DRIVER=vsphere.",
        )
        return report

    if not _check_connection(driver, report):
        return report  # everything below needs a session

    _check_privileges(driver, report)
    _check_regions(session, driver, report)
    _check_templates(session, driver, report)
    _check_networks(session, driver, report)
    return report


# --- checks -----------------------------------------------------------------


def _check_catalog(session: Session, report: Report) -> None:
    regions = session.execute(select(Region).where(Region.is_active.is_(True))).scalars().all()
    if not regions:
        report.fail(
            "Catalog: regions",
            "no active regions",
            "Run `hostplane seed`, or add a region in /admin/catalog.",
        )
        return
    report.ok("Catalog: regions", f"{len(regions)} active")

    plans = session.execute(select(Plan).where(Plan.is_active.is_(True))).scalars().all()
    if not plans:
        report.fail("Catalog: plans", "no active plans", "Publish at least one plan.")
    else:
        report.ok("Catalog: plans", f"{len(plans)} active")

    images = session.execute(select(OsImage).where(OsImage.is_active.is_(True))).scalars().all()
    if not images:
        report.fail("Catalog: images", "no active images", "Publish at least one OS image.")
    else:
        report.ok("Catalog: images", f"{len(images)} active")

    for region in regions:
        available = ipam.region_available_count(session, region.id)
        if available == 0:
            report.fail(
                f"IP capacity: {region.slug}",
                "no allocatable addresses",
                "Add a pool at /admin/pools, or wait for quarantined addresses to age out.",
            )
        elif available < 5:
            report.warn(
                f"IP capacity: {region.slug}",
                f"only {available} addresses left",
                "Add another pool before this region runs dry.",
            )
        else:
            report.ok(f"IP capacity: {region.slug}", f"{available} available")


def _check_connection(driver: object, report: Report) -> bool:
    try:
        si = driver.connect()  # type: ignore[attr-defined]
        about = si.content.about
        report.ok(
            "vCenter connection",
            f"{about.fullName} as {driver.user}",  # type: ignore[attr-defined]
        )
        if not driver.verify_ssl:  # type: ignore[attr-defined]
            report.warn(
                "TLS verification",
                "certificate verification is DISABLED",
                "Set HOSTPLANE_VSPHERE_VERIFY_SSL=true and point "
                "HOSTPLANE_VSPHERE_CA_BUNDLE at the signing CA. Production refuses "
                "to boot with verification off.",
            )
        else:
            report.ok("TLS verification", "enabled")
        return True
    except DriverError as exc:
        remedy = (
            "Point HOSTPLANE_VSPHERE_CA_BUNDLE at the CA that signed vCenter's certificate."
            if "TLS" in exc.message
            else "Check HOSTPLANE_VSPHERE_HOST, _USER and _PASSWORD, and that the "
            "host is reachable from this network."
        )
        report.fail("vCenter connection", exc.message, remedy)
        return False


def _check_privileges(driver: object, report: Report) -> None:
    """Compare the session user's effective rights against what the driver uses."""
    required = list(REQUIRED_PRIVILEGES)
    try:
        si = driver.connect()  # type: ignore[attr-defined]
        content = si.content
        session_id = content.sessionManager.currentSession.key

        # Returns a bool per requested privilege, positionally aligned with the
        # input list.
        results = content.authorizationManager.HasPrivilegeOnEntity(
            entity=content.rootFolder, sessionId=session_id, privId=required
        )
        granted = {priv for priv, has in zip(required, results or [], strict=False) if has}
    except Exception as exc:
        report.warn(
            "Privileges",
            f"could not be verified: {exc}",
            "Grant the service account read access to the authorization manager, "
            "or check privileges by hand against docs/vcenter-setup.md.",
        )
        return

    missing = [p for p in required if p not in granted]
    if not missing:
        report.ok("Privileges", f"all {len(required)} required privileges granted")
        return

    for priv in missing:
        report.fail(
            f"Privilege: {priv}",
            f"not granted — needed to {REQUIRED_PRIVILEGES[priv]}",
            "Add it to the service account's role at the datacenter, with "
            "'propagate to children' enabled.",
        )


def _check_regions(session: Session, driver: object, report: Report) -> None:
    """Every inventory name a region references must resolve."""
    from pyVmomi import vim

    for region in session.execute(select(Region).where(Region.is_active.is_(True))).scalars():
        label = f"Region {region.slug}"

        datacenter = None
        if region.vsphere_datacenter:
            datacenter = driver._find_object(  # type: ignore[attr-defined]
                vim.Datacenter, region.vsphere_datacenter
            )
            if datacenter is None:
                report.fail(
                    f"{label}: datacenter",
                    f"'{region.vsphere_datacenter}' not found",
                    "Correct regions.vsphere_datacenter to the exact inventory name.",
                )
                continue
            report.ok(f"{label}: datacenter", region.vsphere_datacenter)

        if not region.vsphere_cluster and not region.vsphere_resource_pool:
            report.fail(
                f"{label}: placement",
                "neither a cluster nor a resource pool is set",
                "Set regions.vsphere_cluster or regions.vsphere_resource_pool; "
                "the driver has no placement target without one.",
            )
        else:
            for attr, vim_type, kind in (
                ("vsphere_cluster", vim.ClusterComputeResource, "cluster"),
                ("vsphere_resource_pool", vim.ResourcePool, "resource pool"),
            ):
                name = getattr(region, attr)
                if not name:
                    continue
                found = driver._find_object(vim_type, name, root=datacenter)  # type: ignore[attr-defined]
                if found is None:
                    report.fail(
                        f"{label}: {kind}", f"'{name}' not found", f"Correct regions.{attr}."
                    )
                else:
                    report.ok(f"{label}: {kind}", name)

        if region.vsphere_datastore:
            datastore = driver._find_object(  # type: ignore[attr-defined]
                vim.Datastore, region.vsphere_datastore, root=datacenter
            )
            if datastore is None:
                report.fail(
                    f"{label}: datastore",
                    f"'{region.vsphere_datastore}' not found",
                    "Correct regions.vsphere_datastore.",
                )
            else:
                _check_datastore_capacity(session, region, datastore, report, label)

        if region.vsphere_folder:
            folder = driver._find_object(  # type: ignore[attr-defined]
                vim.Folder, region.vsphere_folder, root=datacenter
            )
            if folder is None:
                report.fail(
                    f"{label}: folder",
                    f"'{region.vsphere_folder}' not found",
                    "Correct regions.vsphere_folder, or clear it to use the "
                    "datacenter's default VM folder.",
                )
            else:
                report.ok(f"{label}: folder", region.vsphere_folder)


def _check_datastore_capacity(
    session: Session, region: Region, datastore: object, report: Report, label: str
) -> None:
    """Free space against the largest plan offered in this region."""
    free = getattr(getattr(datastore, "summary", None), "freeSpace", None)

    largest = (
        session.execute(
            select(Plan)
            .join(PlanRegion, PlanRegion.plan_id == Plan.id)
            .where(
                PlanRegion.region_id == region.id,
                PlanRegion.is_available.is_(True),
                Plan.is_active.is_(True),
            )
            .order_by(Plan.disk_gb.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )

    if free is None or largest is None:
        report.ok(f"{label}: datastore", region.vsphere_datastore or "")
        return

    needed = largest.disk_gb * (1024**3)
    if free < needed:
        report.fail(
            f"{label}: datastore capacity",
            f"{_gb(free)} free, but plan '{largest.slug}' needs {largest.disk_gb} GB",
            "Free space, add a datastore, or withdraw the plan in this region.",
        )
    elif free < needed * 3:
        report.warn(
            f"{label}: datastore capacity",
            f"{_gb(free)} free — room for fewer than 3 '{largest.slug}' servers",
            "Plan additional capacity.",
        )
    else:
        report.ok(f"{label}: datastore capacity", f"{_gb(free)} free")


def _check_templates(session: Session, driver: object, report: Report) -> None:
    """Every active image must map to a usable, correctly-sized template."""
    from pyVmomi import vim

    for image in session.execute(select(OsImage).where(OsImage.is_active.is_(True))).scalars():
        label = f"Template {image.slug}"

        if not image.vsphere_template:
            report.fail(
                label,
                "os_images.vsphere_template is empty",
                "Set it to the template's inventory name, e.g. 'tmpl-ubuntu-24.04'.",
            )
            continue

        vm = driver._find_object(vim.VirtualMachine, image.vsphere_template)  # type: ignore[attr-defined]
        if vm is None:
            report.fail(
                label,
                f"'{image.vsphere_template}' not found in inventory",
                "Create the template (see docs/vcenter-setup.md) or correct the name.",
            )
            continue

        config = getattr(vm, "config", None)
        if config is not None and not getattr(config, "template", False):
            report.warn(
                label,
                f"'{image.vsphere_template}' exists but is a VM, not a template",
                "Convert it to a template so it cannot be powered on accidentally.",
            )

        hardware = getattr(config, "hardware", None) if config else None
        if hardware is None:
            report.warn(label, "hardware could not be inspected")
            continue

        nics = [d for d in hardware.device if isinstance(d, vim.vm.device.VirtualEthernetCard)]
        if not nics:
            report.fail(
                f"{label}: NIC",
                "template has no network adapter",
                "Add one NIC to the template; the driver re-points it at the pool's port group.",
            )
        elif len(nics) > 1:
            report.warn(
                f"{label}: NIC",
                f"{len(nics)} adapters — only the first is configured",
                "Remove the extras, or the guest will have unconfigured interfaces.",
            )
        else:
            report.ok(f"{label}: NIC", "1 adapter")

        disks = [d for d in hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        if not disks:
            report.fail(f"{label}: disk", "template has no virtual disk", "Add a system disk.")
            continue

        template_gb = disks[0].capacityInKB // (1024 * 1024)
        smallest = (
            session.execute(
                select(Plan)
                .where(Plan.is_active.is_(True), Plan.disk_gb >= image.min_disk_gb)
                .order_by(Plan.disk_gb)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if smallest is not None and template_gb > smallest.disk_gb:
            # Disks can grow but never shrink, so this is a hard blocker on the
            # smallest plan rather than a warning.
            report.fail(
                f"{label}: disk size",
                f"template disk is {template_gb} GB but plan '{smallest.slug}' "
                f"offers only {smallest.disk_gb} GB",
                "Shrink the template's disk, or withdraw the plan — disks cannot be shrunk.",
            )
        else:
            report.ok(f"{label}: disk size", f"{template_gb} GB")


def _check_networks(session: Session, driver: object, report: Report) -> None:
    """Every port group referenced by a pool or region must resolve."""
    from pyVmomi import vim

    seen: set[str] = set()

    for pool in session.execute(select(IpPool).where(IpPool.is_active.is_(True))).scalars():
        region = session.get(Region, pool.region_id)
        name = pool.vsphere_network or (region.vsphere_network if region else None)

        if not name:
            report.fail(
                f"Port group: pool {pool.cidr}",
                "no port group set on the pool or its region",
                "Set ip_pools.vsphere_network or regions.vsphere_network.",
            )
            continue

        if name in seen:
            continue
        seen.add(name)

        network = driver._find_object(vim.Network, name)  # type: ignore[attr-defined]
        if network is None:
            report.fail(
                f"Port group: {name}",
                "not found in inventory",
                "Correct the port group name, or create it on the switch.",
            )
            continue

        if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
            switch = getattr(getattr(network, "config", None), "distributedVirtualSwitch", None)
            if switch is None:
                report.fail(
                    f"Port group: {name}",
                    "distributed port group has no backing switch",
                    "Re-attach the port group to its distributed switch.",
                )
                continue
            report.ok(f"Port group: {name}", "distributed")
        else:
            report.ok(f"Port group: {name}", "standard")
