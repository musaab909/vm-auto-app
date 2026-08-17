"""vSphere driver tests.

Scope note, stated plainly: these cover the parts that can be verified without a
vCenter — cloud-init payload generation, inventory-lookup failure handling, and
task-fault classification. They do **not** prove that a real `CloneVM_Task`
succeeds, that a real guest consumes the guestinfo payload, or that WebMKS
tickets work. Those require the operator's own vSphere environment; see
`docs/vcenter-setup.md` for the manual checks.

The cloud-init builders get the most attention because they carry real risk: a
malformed network-config yields a VM that boots with no network and can only be
recovered through the console.
"""

from __future__ import annotations

import base64
import types

import pytest

from app.drivers import vsphere as vsphere_module
from app.drivers.base import DriverInstance, InstanceSpec, NetworkSpec, PowerState
from app.drivers.vsphere import (
    GUEST_KEY_ACCOUNT_ID,
    GUEST_KEY_INSTANCE_ID,
    VSphereDriver,
    build_metadata,
    build_network_config,
    build_userdata,
)
from app.services.errors import DriverError


def make_spec(**overrides) -> InstanceSpec:
    network = overrides.pop(
        "network",
        NetworkSpec(
            ip_address="203.0.113.10",
            prefix_length=24,
            gateway="203.0.113.1",
            dns_servers=["1.1.1.1", "8.8.8.8"],
            network_name="VM Network",
        ),
    )
    params = {
        "name": "web-01-abc123",
        "hostname": "web-01",
        "vcpu": 2,
        "ram_mb": 4096,
        "disk_gb": 50,
        "template": "tmpl-ubuntu-24.04",
        "network": network,
        "cluster": "Cluster-01",
        "datastore": "ds1",
        "root_password": "s3cret-password",
        "instance_id": "inst-123",
        "account_id": "acct-456",
    }
    params.update(overrides)
    return InstanceSpec(**params)


# --- cloud-init: network ----------------------------------------------------


def test_network_config_sets_a_static_address():
    config = build_network_config(make_spec())

    assert "version: 2" in config
    assert "dhcp4: false" in config
    assert "- 203.0.113.10/24" in config


def test_network_config_uses_routes_not_the_deprecated_gateway4():
    """`gateway4:` is deprecated in netplan and warns on modern Ubuntu."""
    config = build_network_config(make_spec())

    assert "gateway4" not in config
    assert "to: default" in config
    assert "via: 203.0.113.1" in config


def test_network_config_includes_nameservers():
    config = build_network_config(make_spec())

    assert "nameservers:" in config
    assert "- 1.1.1.1" in config
    assert "- 8.8.8.8" in config


def test_network_config_omits_routes_when_there_is_no_gateway():
    spec = make_spec(
        network=NetworkSpec(ip_address="10.0.0.5", prefix_length=24, gateway=None, dns_servers=[])
    )
    config = build_network_config(spec)

    assert "to: default" not in config
    assert "nameservers:" not in config
    assert "- 10.0.0.5/24" in config


# --- cloud-init: metadata and user-data -------------------------------------


def test_metadata_carries_hostname_and_instance_id():
    metadata = build_metadata(make_spec())

    assert "local-hostname: web-01" in metadata
    assert "instance-id: inst-123" in metadata


def test_userdata_sets_the_root_password():
    userdata = build_userdata(make_spec())

    assert userdata.startswith("#cloud-config")
    assert "chpasswd:" in userdata
    assert "root:s3cret-password" in userdata
    assert "expire: false" in userdata


def test_password_auth_is_enabled_only_without_an_ssh_key():
    """A freshly ordered VPS with no key would be unreachable otherwise."""
    without_key = build_userdata(make_spec(ssh_keys=[]))
    assert "ssh_pwauth: true" in without_key

    with_key = build_userdata(make_spec(ssh_keys=["ssh-ed25519 AAAA... user@host"]))
    assert "ssh_pwauth: false" in with_key


def test_userdata_installs_ssh_keys_for_root():
    userdata = build_userdata(make_spec(ssh_keys=["ssh-ed25519 AAAAKEY user@host"]))

    assert "disable_root: false" in userdata
    assert "ssh_authorized_keys:" in userdata
    assert "- ssh-ed25519 AAAAKEY user@host" in userdata


def test_userdata_creates_a_sudo_user_for_non_root_images():
    userdata = build_userdata(
        make_spec(default_username="ubuntu", ssh_keys=["ssh-ed25519 AAAAKEY user@host"])
    )

    assert "name: ubuntu" in userdata
    assert "sudo: ALL=(ALL) NOPASSWD:ALL" in userdata
    assert "ubuntu:s3cret-password" in userdata


def test_userdata_sets_the_hostname():
    userdata = build_userdata(make_spec())

    assert "hostname: web-01" in userdata
    assert "preserve_hostname: false" in userdata


# --- guestinfo encoding -----------------------------------------------------


def test_guest_extra_config_is_base64_and_declares_its_encoding():
    """cloud-init only decodes guestinfo when the *.encoding key says base64."""
    driver = VSphereDriver.__new__(VSphereDriver)  # no connection needed

    values = {opt.key: opt.value for opt in driver._guest_extra_config(make_spec())}

    assert values["guestinfo.metadata.encoding"] == "base64"
    assert values["guestinfo.userdata.encoding"] == "base64"

    decoded = base64.b64decode(values["guestinfo.userdata"]).decode()
    assert decoded.startswith("#cloud-config")


def test_exact_guestinfo_key_set_is_written():
    """Pin the key set: an extra or missing key here changes guest behaviour.

    `guestinfo.vendordata` must be absent in particular — see
    `test_network_config_travels_inside_metadata`.
    """
    driver = VSphereDriver.__new__(VSphereDriver)

    keys = {opt.key for opt in driver._guest_extra_config(make_spec())}

    assert keys == {
        "guestinfo.metadata",
        "guestinfo.metadata.encoding",
        "guestinfo.userdata",
        "guestinfo.userdata.encoding",
        GUEST_KEY_INSTANCE_ID,
        GUEST_KEY_ACCOUNT_ID,
    }


def test_network_config_travels_inside_metadata():
    """Regression: network config must be a `network` key within the metadata.

    It used to be written to `guestinfo.vendordata`, which cloud-init's VMware
    datasource parses as vendor-data (cloud-config) and discards — the guest then
    boots with no network at all, reachable only via the console.
    """
    driver = VSphereDriver.__new__(VSphereDriver)

    values = {opt.key: opt.value for opt in driver._guest_extra_config(make_spec())}

    assert "guestinfo.vendordata" not in values
    assert "guestinfo.vendordata.encoding" not in values

    metadata = base64.b64decode(values["guestinfo.metadata"]).decode()
    assert "network.encoding: base64" in metadata

    network_line = next(line for line in metadata.splitlines() if line.startswith("network: "))
    network = base64.b64decode(network_line.split(" ", 1)[1]).decode()

    assert "version: 2" in network
    assert "- 203.0.113.10/24" in network
    assert "via: 203.0.113.1" in network


def test_metadata_is_parseable_yaml_with_a_decodable_network():
    """The metadata document must survive a real YAML parse.

    Embedding the netplan document base64-encoded rather than inline is what
    keeps this true — a raw multi-line YAML value would need indentation the
    string builder does not apply.
    """
    yaml = pytest.importorskip("yaml")

    parsed = yaml.safe_load(build_metadata(make_spec()))

    assert parsed["local-hostname"] == "web-01"
    assert parsed["instance-id"] == "inst-123"
    assert parsed["network.encoding"] == "base64"

    network = yaml.safe_load(base64.b64decode(parsed["network"]).decode())
    assert network["version"] == 2
    assert network["ethernets"]["id0"]["addresses"] == ["203.0.113.10/24"]
    assert network["ethernets"]["id0"]["dhcp4"] is False


def test_guest_extra_config_tags_the_vm_for_correlation():
    """Without these keys an orphaned VM cannot be traced back to its owner."""
    driver = VSphereDriver.__new__(VSphereDriver)

    values = {opt.key: opt.value for opt in driver._guest_extra_config(make_spec())}

    assert values[GUEST_KEY_INSTANCE_ID] == "inst-123"
    assert values[GUEST_KEY_ACCOUNT_ID] == "acct-456"


# --- configuration guards ---------------------------------------------------


def test_driver_requires_connection_settings(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "vsphere_host", None)

    with pytest.raises(DriverError) as exc:
        VSphereDriver()
    assert exc.value.permanent is True
    assert "HOSTPLANE_VSPHERE_HOST" in exc.value.message


def test_production_config_rejects_disabled_tls_verification():
    """A misconfigured deployment must fail at boot, not silently trust any cert."""
    from app.config import Settings

    settings = Settings(
        env="production",
        secret_key="a-real-production-secret",
        gateway_webhook_secret="a-real-webhook-secret",
        driver="vsphere",
        vsphere_verify_ssl=False,
    )

    with pytest.raises(RuntimeError, match="VSPHERE_VERIFY_SSL"):
        settings.check_production_safety()


def test_production_config_rejects_development_secrets():
    from app.config import DEV_SECRET_SENTINEL, Settings

    # Passed explicitly rather than relying on the environment, which the test
    # harness populates with its own key.
    settings = Settings(
        env="production",
        secret_key=DEV_SECRET_SENTINEL,
        gateway_webhook_secret="a-real-webhook-secret",
    )

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        settings.check_production_safety()


def test_tls_verification_is_on_by_default():
    from app.config import Settings

    assert Settings().vsphere_verify_ssl is True


# --- inventory failures -----------------------------------------------------


def test_missing_template_fails_permanently(monkeypatch):
    """A missing template will fail identically on every retry, so do not retry."""
    driver = VSphereDriver.__new__(VSphereDriver)
    monkeypatch.setattr(driver, "_find_object", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(type(driver), "content", property(lambda self: None), raising=False)

    with pytest.raises(DriverError) as exc:
        driver._require(object, "tmpl-missing", "Template")

    assert exc.value.permanent is True
    assert "not found in vCenter inventory" in exc.value.message


def test_find_by_instance_id_is_used_for_idempotent_create(monkeypatch):
    """A retried create must adopt the existing VM rather than clone a second one."""
    driver = VSphereDriver.__new__(VSphereDriver)
    sentinel = object()
    monkeypatch.setattr(driver, "find_by_instance_id", lambda instance_id: sentinel, raising=False)

    # create_instance returns the adopted VM before touching any inventory lookup,
    # so no connection is required to prove the short-circuit.
    assert driver.create_instance(make_spec()) is sentinel


# --- inventory lookup behaviour ---------------------------------------------
#
# These use a hand-built fake of the small slice of the vim/vmodl API the lookup
# helpers touch. The point is not to re-test pyvmomi, it is to pin the *shape* of
# the calls: the previous implementation walked every VM in the inventory to
# resolve a MoRef and dereferenced `.config` per VM to find one by instance id.
# Both are invisible on a lab and painful on a populated vCenter, and neither is
# something a unit test would catch without asserting on call counts.


class _FakeStub:
    pass


class _FakeVm:
    def __init__(self, mo_id, extra_config=None, missing=False):
        self._moId = mo_id
        self._missing = missing
        self._extra = extra_config or {}
        self.config_reads = 0

    @property
    def name(self):
        if self._missing:
            raise _FakeManagedObjectNotFound()
        return f"vm-name-{self._moId}"

    @property
    def config(self):
        self.config_reads += 1
        return type("Config", (), {"extraConfig": self._extra_options()})()

    def _extra_options(self):
        return [type("Opt", (), {"key": k, "value": v})() for k, v in self._extra.items()]


class _FakeManagedObjectNotFound(Exception):
    pass


class _FakeMethodFault(Exception):
    pass


class _FakeContainerView:
    def __init__(self, view):
        self.view = view
        self.destroyed = False

    def Destroy(self):
        self.destroyed = True


class _FakePropertyCollector:
    def __init__(self, vms):
        self._vms = vms
        self.retrieve_calls = 0

    def RetrieveContents(self, filter_specs):
        self.retrieve_calls += 1
        results = []
        for vm in self._vms:
            prop = type("Prop", (), {"name": "config.extraConfig", "val": vm._extra_options()})()
            results.append(type("Result", (), {"obj": vm, "propSet": [prop]})())
        return results


def _fake_vim_module(vm_class):
    """A stand-in for the `vim` namespace, exposing only what the code touches."""
    ns = types.SimpleNamespace()
    ns.VirtualMachine = vm_class
    ns.view = types.SimpleNamespace(ContainerView=object)
    return ns


def _fake_vmodl_module():
    ns = types.SimpleNamespace()
    ns.fault = types.SimpleNamespace(ManagedObjectNotFound=_FakeManagedObjectNotFound)
    ns.MethodFault = _FakeMethodFault
    ns.query = types.SimpleNamespace(
        PropertyCollector=types.SimpleNamespace(
            TraversalSpec=lambda **kw: kw,
            ObjectSpec=lambda **kw: kw,
            PropertySpec=lambda **kw: kw,
            FilterSpec=lambda **kw: kw,
        )
    )
    return ns


def _driver_with_fake_session(monkeypatch, vms, vm_factory=None):
    """A driver whose `connect()` returns a fake ServiceInstance over `vms`."""
    collector = _FakePropertyCollector(vms)
    container = _FakeContainerView(vms)

    content = types.SimpleNamespace(
        rootFolder=object(),
        viewManager=types.SimpleNamespace(CreateContainerView=lambda *a, **k: container),
        propertyCollector=collector,
        searchIndex=types.SimpleNamespace(
            FindByUuid=lambda **kw: next(
                (v for v in vms if v._extra.get("uuid") == kw.get("uuid")), None
            )
        ),
    )
    si = types.SimpleNamespace(content=content, _stub=_FakeStub())

    driver = VSphereDriver.__new__(VSphereDriver)
    monkeypatch.setattr(driver, "connect", lambda: si, raising=False)
    monkeypatch.setattr(vsphere_module, "vmodl", _fake_vmodl_module())
    monkeypatch.setattr(
        vsphere_module, "vim", _fake_vim_module(vm_factory or (lambda ref, stub: _FakeVm(ref)))
    )
    return driver, container, collector


def test_moref_resolution_makes_no_container_view(monkeypatch):
    """Resolving a MoRef must wrap it directly, not scan the inventory."""
    vms = [_FakeVm(f"vm-{i}") for i in range(50)]
    constructed = []

    def factory(ref, stub):
        constructed.append(ref)
        return _FakeVm(ref)

    driver, container, collector = _driver_with_fake_session(monkeypatch, vms, factory)

    vm = driver._find_vm_by_ref("vm-42")

    assert vm is not None
    assert constructed == ["vm-42"], "the ref should be wrapped, not searched for"
    assert container.destroyed is False, "no container view should have been built"
    assert collector.retrieve_calls == 0


def test_moref_resolution_returns_none_for_a_deleted_vm(monkeypatch):
    """A stale ref must read as absent rather than raising."""
    driver, _, _ = _driver_with_fake_session(
        monkeypatch, [], vm_factory=lambda ref, stub: _FakeVm(ref, missing=True)
    )

    assert driver._find_vm_by_ref("vm-gone") is None


def test_find_by_instance_id_uses_one_property_collector_call(monkeypatch):
    """One bulk retrieval, not one round trip per VM."""
    vms = [_FakeVm(f"vm-{i}", {GUEST_KEY_INSTANCE_ID: f"inst-{i}"}) for i in range(50)]
    driver, container, collector = _driver_with_fake_session(monkeypatch, vms)
    monkeypatch.setattr(
        driver,
        "_to_driver_instance",
        lambda vm: DriverInstance(ref=vm._moId, power_state=PowerState.ON),
    )

    found = driver.find_by_instance_id("inst-7")

    assert found is not None and found.ref == "vm-7"
    assert collector.retrieve_calls == 1, "should be a single bulk retrieval"
    assert all(vm.config_reads == 0 for vm in vms), "must not dereference .config per VM"
    assert container.destroyed is True, "the container view must be cleaned up"


def test_find_by_instance_id_returns_none_when_absent(monkeypatch):
    vms = [_FakeVm(f"vm-{i}", {GUEST_KEY_INSTANCE_ID: f"inst-{i}"}) for i in range(5)]
    driver, container, _ = _driver_with_fake_session(monkeypatch, vms)

    assert driver.find_by_instance_id("inst-nope") is None
    assert container.destroyed is True
