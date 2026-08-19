# vCenter setup

What must exist in vSphere before `HOSTPLANE_DRIVER=vsphere` will provision
anything, and how to verify it.

None of this was executed against a live vCenter while the driver was written —
there was none reachable. Treat the checklist at the end as required work, not
as a formality.

## Start here: `hostplane doctor`

Most of this page is automated. Configure the connection settings, then:

```bash
hostplane doctor
```

It checks connectivity and TLS, enumerates the service account's privileges
against what the driver actually uses, resolves every inventory name your regions
and pools reference, inspects each template, and compares datastore free space
against your largest plan. Every failure prints the specific fix, and the command
exits non-zero so it can gate a deploy.

To see the exact cloud-init documents a real order would inject, without touching
vCenter at all:

```bash
hostplane doctor --dry-run
```

Work through `doctor` until it is clean, then run the manual checklist at the end
of this page — those are the things no preflight can prove.

## 1. Service account

Create a dedicated vCenter user (do not reuse `administrator@vsphere.local`) and
grant a role with at least:

| Area | Privileges |
| --- | --- |
| Virtual machine → Provisioning | Clone virtual machine, Deploy template, Customize guest |
| Virtual machine → Configuration | Change CPU count, Change memory, Modify device settings, Change settings (needed for `extraConfig`), Extend virtual disk |
| Virtual machine → Interaction | Power on, Power off, Reset, Console interaction |
| Virtual machine → Inventory | Create from existing, Remove |
| Resource | Assign virtual machine to resource pool |
| Datastore | Allocate space, Browse datastore |
| Network | Assign network |

`Change settings` is the one most often missed, and without it the guestinfo keys
never land — the VM clones successfully and then boots with no network and no
credentials.

Assign the role at the datacenter with **propagate to children** enabled.

## 2. TLS

The driver verifies certificates and there is no implicit escape hatch;
production config refuses to boot with verification off.

If vCenter uses a private CA, export the chain and point the app at it:

```bash
curl -sk https://vcenter.example.com/certs/download.zip -o certs.zip
unzip certs.zip && cat certs/lin/*.0 > /etc/ssl/hostplane/vcenter-ca.pem
```

```bash
HOSTPLANE_VSPHERE_CA_BUNDLE=/etc/ssl/hostplane/vcenter-ca.pem
```

A `DriverError` mentioning TLS verification means the bundle is wrong or missing
— it is never a reason to set `HOSTPLANE_VSPHERE_VERIFY_SSL=false` outside a lab.

## 3. Guest templates

Guest configuration is injected with cloud-init's **NoCloud datasource over
guestinfo**, not VMware customization specs. Customization specs are the legacy
path: they need Perl in the guest, serialise poorly across concurrent clones, and
cannot express arbitrary user-data.

For each OS you want to offer, build a template:

```bash
# Ubuntu / Debian
apt-get update
apt-get install -y cloud-init open-vm-tools

# RHEL family
dnf install -y cloud-init open-vm-tools
```

Enable the VMware datasource and disable the others so cloud-init does not stall
on network probes at boot:

```bash
cat >/etc/cloud/cloud.cfg.d/99-hostplane.cfg <<'EOF'
datasource_list: [ VMware, NoCloud, None ]
datasource:
  VMware:
    allow_raw_data: true
EOF
```

Clean state before converting to a template, or every clone inherits the same
machine-id, SSH host keys and cloud-init instance id:

```bash
cloud-init clean --logs --seed
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*
shutdown -h now
```

Then convert the VM to a template and record its inventory name in the
`os_images.vsphere_template` column (the seed CLI uses names like
`tmpl-ubuntu-24.04`).

Verify cloud-init version ≥ 21.3 (`cloud-init --version`) — earlier releases
lack the VMware datasource this depends on.

### How the guest is configured

Two guestinfo keys are written at clone time, both base64-encoded with a matching
`.encoding` key:

| Key | Contents |
| --- | --- |
| `guestinfo.metadata` | instance-id, hostname, **and the network configuration** under a `network` key |
| `guestinfo.userdata` | cloud-config: password, SSH keys, hostname |

Network configuration lives *inside the metadata document*, not in
`guestinfo.vendordata`. The datasource reads vendordata as vendor-data
(cloud-config), so a netplan document placed there is parsed and discarded, and
the guest boots with no network. No vendordata key is written at all.

`hostplane doctor --dry-run` prints both documents, fully decoded, so you can
check them before creating anything.

### Console access

`HOSTPLANE_CONSOLE_MODE` picks how the browser reaches a VM's screen:

| Mode | Needs | Trade-off |
| --- | --- | --- |
| `vcenter` (default) | nothing extra | Works immediately; the customer sees the vCenter hostname |
| `webmks` | VMware `wmks.js` | Fully self-hosted; the SDK is not redistributable so you must supply it |

For `webmks`, download the vSphere HTML Console SDK from Broadcom, serve
`wmks.js` from your own static host, and set `HOSTPLANE_CONSOLE_WMKS_PATH` to its
URL. Without it the console page explains what is missing instead of rendering a
blank canvas.

Either way, `VirtualMachine.Interact.ConsoleInteract` must be granted — `doctor`
checks for it.

## 4. Networking

Create the port group your customers' VMs attach to, on a standard vSwitch or a
distributed switch. The driver handles both — distributed port groups are
addressed by switch UUID and port key rather than by name.

Record the port group name on either the region (`regions.vsphere_network`) or
the pool (`ip_pools.vsphere_network`, which takes precedence).

## 5. Control plane configuration

Region rows map to vSphere inventory by name, resolved at runtime. MoRefs are
deliberately not stored — they change when inventory is recreated.

```sql
UPDATE regions SET
  vsphere_datacenter = 'DC-Frankfurt',
  vsphere_cluster    = 'Cluster-01',
  vsphere_datastore  = 'vsanDatastore',
  vsphere_network    = 'VM Network',
  vsphere_folder     = NULL
WHERE slug = 'eu-central';
```

Either `vsphere_cluster` or `vsphere_resource_pool` must be set; without one the
driver has no placement target and fails permanently.

Then replace the seeded documentation-range IP pool with real addresses. The
default pool uses `203.0.113.0/24` (RFC 5737) precisely so a fresh install cannot
hand out somebody else's addresses. Use `/admin/pools` or:

```bash
python -m app.cli capacity   # confirm what is configured
```

## 6. Verification checklist

Run these against your own environment, after `hostplane doctor` is clean. Each
is something neither the preflight nor the automated tests can prove — they all
require a guest to actually boot.

If you have a scratch cluster, the gated integration suite automates the
hypervisor-side half of this list:

```bash
HOSTPLANE_VSPHERE_ITEST_CONFIRM=1 \
HOSTPLANE_VSPHERE_ITEST_TEMPLATE=tmpl-ubuntu-24.04 \
HOSTPLANE_VSPHERE_ITEST_CLUSTER=Cluster-01 \
HOSTPLANE_VSPHERE_ITEST_DATASTORE=vsanDatastore \
HOSTPLANE_VSPHERE_ITEST_NETWORK='VM Network' \
pytest -m vcenter -v
```

It creates and destroys real VMs named `hostplane-itest-*`.

- [ ] `hostplane doctor` reports no failures.
- [ ] `hostplane doctor --dry-run` shows a metadata document whose decoded
      `network` key has the right address, gateway and DNS.
- [ ] Order a server through the portal; it reaches `running` within a few
      minutes.
- [ ] The cloned VM has the right vCPU, memory and disk size — the disk was
      extended to the plan size, not left at template size.
- [ ] The guest booted with the **static IP** the control plane allocated, and
      the gateway and DNS are correct (`ip addr`, `ip route`, `resolvectl`).
- [ ] `ssh root@<ip>` works with the password shown once at order time, or with
      the SSH key if one was selected.
- [ ] The hostname inside the guest matches the order.
- [ ] Stop, start and reboot from the portal each take effect in vCenter.
- [ ] Power a VM off directly in vCenter, wait for the reconciler (or run
      `python -m app.cli reconcile`), and confirm the portal shows `stopped`.
- [ ] Open the console from the portal and confirm it connects. With
      `HOSTPLANE_CONSOLE_MODE=vcenter` this hands off to vCenter's own console;
      with `webmks` it needs `wmks.js` supplied at
      `HOSTPLANE_CONSOLE_WMKS_PATH` (see below).
- [ ] Open the same console link a second time — it must be refused. Console
      handles are single-use by design.
- [ ] Order more servers at once than `regions.max_concurrent_clones` allows and
      confirm the extras are *deferred and retried*, not failed. `/admin` shows
      slots in use per region.
- [ ] Delete the server; confirm the VM is gone from vCenter and its IP returns
      to the pool (`python -m app.cli capacity`).
- [ ] Reinstall a server and confirm it comes back with a fresh disk and a new
      root password.

## Troubleshooting

**VM boots with no network.** cloud-init did not consume the guestinfo payload.
Run `hostplane doctor` first — a missing `Change settings`
(`VirtualMachine.Config.AdvancedConfig`) privilege is by far the most common
cause, and it fails silently: the clone succeeds and the keys simply never land.

Then check inside the guest:

```bash
vmware-rpctool "info-get guestinfo.metadata" | base64 -d   # should show a network: key
cloud-init query --all | head                              # what the datasource saw
journalctl -u cloud-init --no-pager | tail -50
```

If the metadata key is absent, it is the privilege. If it is present but the
guest still has no address, it is the datasource config or the cloud-init
version.

**`Clone request rejected` immediately.** Usually a missing privilege or a name
collision. The driver marks `InvalidArgument`, `NotFound`, `DuplicateName`,
`InvalidState` and `InsufficientResources` as permanent, so it will not retry —
the error text in the jobs table is the vCenter fault message verbatim.

**Clone times out.** `HOSTPLANE_VSPHERE_TASK_TIMEOUT_SECONDS` defaults to 1800.
Thick-provisioned clones of large templates across slow storage can exceed it.

**Instances stuck in `provisioning`.** Check the worker is running and look at
`/admin/jobs`. `requeue_stuck_jobs` recovers jobs orphaned by a dead worker after
30 minutes.
