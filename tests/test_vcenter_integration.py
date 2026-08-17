"""Integration tests against a real vCenter.

**These create and destroy real virtual machines.** They are skipped unless two
environment variables are set together:

    HOSTPLANE_VSPHERE_HOST=vcenter.example.com   # plus _USER / _PASSWORD
    HOSTPLANE_VSPHERE_ITEST_CONFIRM=1

The second is a deliberate speed bump. Connection settings alone are not taken as
consent to clone VMs — a developer with a working `.env` should be able to run
`pytest` without discovering it just built and destroyed machines on a live
cluster.

Run explicitly:

    HOSTPLANE_VSPHERE_ITEST_CONFIRM=1 pytest -m vcenter -v

Everything created is named with `ITEST_PREFIX` and destroyed in a `finally`, so
a stray VM after a crashed run is identifiable and safe to delete by hand.

What this covers that the unit tests cannot: that vCenter accepts the CloneSpec
the driver builds, that guestinfo keys survive the clone, and that power actions
and destroy behave against real inventory. What it still does not cover: whether
the guest *booted* and consumed cloud-init. That needs a reachable IP and an SSH
probe, which depends on the operator's network — see docs/vcenter-setup.md.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.drivers.base import InstanceSpec, NetworkSpec, PowerState
from app.services.errors import DriverError

pytestmark = pytest.mark.vcenter

ITEST_PREFIX = "hostplane-itest-"

_HAS_CREDENTIALS = bool(
    os.environ.get("HOSTPLANE_VSPHERE_HOST")
    and os.environ.get("HOSTPLANE_VSPHERE_USER")
    and os.environ.get("HOSTPLANE_VSPHERE_PASSWORD")
)
_CONFIRMED = os.environ.get("HOSTPLANE_VSPHERE_ITEST_CONFIRM") == "1"

requires_vcenter = pytest.mark.skipif(
    not (_HAS_CREDENTIALS and _CONFIRMED),
    reason=(
        "needs HOSTPLANE_VSPHERE_HOST/_USER/_PASSWORD and "
        "HOSTPLANE_VSPHERE_ITEST_CONFIRM=1 — these tests create real VMs"
    ),
)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} must be set to run the vCenter integration tests")
    return value


@pytest.fixture(scope="module")
def driver():
    """A live driver, with the session closed at the end of the module."""
    from app.drivers.vsphere import VSphereDriver

    instance = VSphereDriver()
    try:
        instance.connect()
    except DriverError as exc:
        pytest.skip(f"cannot reach vCenter: {exc.message}")
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def itest_spec() -> InstanceSpec:
    """Placement for the throwaway VM, taken entirely from the environment.

    Deliberately not read from the database: these tests must be runnable against
    a scratch cluster without seeding a catalog that points at it.
    """
    return InstanceSpec(
        name=f"{ITEST_PREFIX}{uuid.uuid4().hex[:8]}",
        hostname="itest",
        vcpu=1,
        ram_mb=1024,
        disk_gb=int(os.environ.get("HOSTPLANE_VSPHERE_ITEST_DISK_GB", "20")),
        template=_env("HOSTPLANE_VSPHERE_ITEST_TEMPLATE"),
        network=NetworkSpec(
            ip_address=os.environ.get("HOSTPLANE_VSPHERE_ITEST_IP", "203.0.113.200"),
            prefix_length=int(os.environ.get("HOSTPLANE_VSPHERE_ITEST_PREFIX_LEN", "24")),
            gateway=os.environ.get("HOSTPLANE_VSPHERE_ITEST_GATEWAY"),
            dns_servers=["1.1.1.1"],
            network_name=os.environ.get("HOSTPLANE_VSPHERE_ITEST_NETWORK"),
        ),
        datacenter=os.environ.get("HOSTPLANE_VSPHERE_ITEST_DATACENTER"),
        cluster=os.environ.get("HOSTPLANE_VSPHERE_ITEST_CLUSTER"),
        resource_pool=os.environ.get("HOSTPLANE_VSPHERE_ITEST_RESOURCE_POOL"),
        datastore=os.environ.get("HOSTPLANE_VSPHERE_ITEST_DATASTORE"),
        root_password="Itest-" + uuid.uuid4().hex[:12],
        ssh_keys=[],
        default_username="root",
        instance_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
    )


# --- connectivity -----------------------------------------------------------


@requires_vcenter
def test_connects_with_tls_verification(driver):
    si = driver.connect()
    assert si.content.about.apiType == "VirtualCenter"
    assert driver.verify_ssl, "integration runs should not disable TLS verification"


@requires_vcenter
def test_preflight_reports_no_vcenter_failures(driver):
    """The doctor must agree the hypervisor side is usable before we clone into it.

    Catalog and IP-capacity checks are excluded: this suite runs against the
    empty test database, so those would fail for reasons that say nothing about
    vCenter. Run `hostplane doctor` against the real database to cover them.
    """
    from app.db import session_scope
    from app.drivers.preflight import run_preflight

    with session_scope() as session:
        report = run_preflight(session, driver)

    catalog_prefixes = ("Catalog:", "IP capacity:")
    failures = [
        f"{c.name}: {c.detail}" for c in report.failures if not c.name.startswith(catalog_prefixes)
    ]
    assert not failures, "preflight failed:\n" + "\n".join(failures)


# --- lifecycle --------------------------------------------------------------


@requires_vcenter
def test_full_lifecycle_against_real_vcenter(driver, itest_spec):
    """Clone, inspect, power-cycle and destroy one real VM.

    Written as a single test rather than several: each step depends on the last,
    and splitting them would risk leaving a VM behind if an early one failed.
    """
    created = None
    try:
        created = driver.create_instance(itest_spec)

        assert created.ref, "clone returned no MoRef"
        assert created.uuid, "clone returned no instance UUID"
        assert created.power_state is PowerState.ON
        assert created.vcpu == itest_spec.vcpu
        assert created.ram_mb == itest_spec.ram_mb
        assert created.disk_gb is not None and created.disk_gb >= itest_spec.disk_gb

        # The guestinfo keys are the whole point of the clone spec; if they did
        # not land, the guest will never configure itself.
        _assert_guestinfo_landed(driver, created.ref, itest_spec)

        # O(1) lookup by instance UUID.
        by_uuid = driver.find_by_uuid(created.uuid)
        assert by_uuid is not None and by_uuid.ref == created.ref

        # Crash-recovery adoption path: locate the VM with no ref recorded.
        adopted = driver.find_by_instance_id(itest_spec.instance_id)
        assert adopted is not None and adopted.ref == created.ref

        # A second create with the same instance id must adopt, not clone again.
        again = driver.create_instance(itest_spec)
        assert again.ref == created.ref, "create_instance cloned a duplicate VM"

        driver.power_off(created.ref, hard=True)
        assert driver.get_instance(created.ref).power_state is PowerState.OFF

        driver.power_on(created.ref)
        assert driver.get_instance(created.ref).power_state is PowerState.ON

    finally:
        if created is not None:
            driver.delete_instance(created.ref)
            assert driver.get_instance(created.ref).exists is False


def _assert_guestinfo_landed(driver, ref: str, spec: InstanceSpec) -> None:
    """Read extraConfig back off the created VM and verify the payload."""
    import base64

    from app.drivers.vsphere import GUEST_KEY_INSTANCE_ID

    vm = driver._require_vm(ref)
    options = {o.key: o.value for o in (vm.config.extraConfig or [])}

    assert "guestinfo.metadata" in options, (
        "guestinfo.metadata is missing — the service account most likely lacks "
        "VirtualMachine.Config.AdvancedConfig"
    )
    assert options.get("guestinfo.metadata.encoding") == "base64"
    assert options.get(GUEST_KEY_INSTANCE_ID) == spec.instance_id
    assert "guestinfo.vendordata" not in options

    metadata = base64.b64decode(options["guestinfo.metadata"]).decode()
    assert f"local-hostname: {spec.hostname}" in metadata
    assert "network.encoding: base64" in metadata

    network_line = next(line for line in metadata.splitlines() if line.startswith("network: "))
    network = base64.b64decode(network_line.split(" ", 1)[1]).decode()
    assert spec.network.ip_address in network


@requires_vcenter
def test_deleting_an_absent_vm_is_a_noop(driver):
    """Delete must be idempotent: a retried destroy is not an error."""
    driver.delete_instance("vm-does-not-exist-99999")


@requires_vcenter
def test_get_instance_reports_a_missing_vm(driver):
    result = driver.get_instance("vm-does-not-exist-99999")
    assert result.exists is False
    assert result.power_state is PowerState.UNKNOWN


@requires_vcenter
def test_missing_template_fails_permanently(driver, itest_spec):
    """A bad template must not be retried — it will fail identically forever."""
    from dataclasses import replace

    bad = replace(
        itest_spec,
        template=f"{ITEST_PREFIX}definitely-not-a-real-template",
        instance_id=str(uuid.uuid4()),
        name=f"{ITEST_PREFIX}{uuid.uuid4().hex[:8]}",
    )

    with pytest.raises(DriverError) as exc:
        driver.create_instance(bad)
    assert exc.value.permanent is True
