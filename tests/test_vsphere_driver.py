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

import pytest

from app.drivers.base import InstanceSpec, NetworkSpec
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


def test_guest_extra_config_is_base64_and_declares_its_encoding(monkeypatch):
    """cloud-init only decodes guestinfo when the *.encoding key says base64."""
    driver = VSphereDriver.__new__(VSphereDriver)  # no connection needed
    spec = make_spec()

    options = driver._guest_extra_config(spec)
    values = {opt.key: opt.value for opt in options}

    assert values["guestinfo.metadata.encoding"] == "base64"
    assert values["guestinfo.userdata.encoding"] == "base64"
    assert values["guestinfo.vendordata.encoding"] == "base64"

    decoded = base64.b64decode(values["guestinfo.userdata"]).decode()
    assert decoded.startswith("#cloud-config")

    decoded_net = base64.b64decode(values["guestinfo.vendordata"]).decode()
    assert "203.0.113.10/24" in decoded_net


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
