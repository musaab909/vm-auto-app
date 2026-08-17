# vCenter setup

What must exist in vSphere before `HOSTPLANE_DRIVER=vsphere` will provision
anything, and how to verify it.

None of this was executed against a live vCenter while the driver was written —
there was none reachable. Treat the checklist at the end as required work, not
as a formality.

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

Verify cloud-init version ≥ 21.3 — earlier releases do not read
`guestinfo.vendordata` for network configuration.

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

Run these against your own environment. Each is something the automated tests
cannot cover.

- [ ] `python -c "from app.drivers.vsphere import VSphereDriver; VSphereDriver().connect()"`
      connects with TLS verification on.
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
- [ ] Open the console from the portal and confirm the WebMKS ticket connects.
- [ ] Delete the server; confirm the VM is gone from vCenter and its IP returns
      to the pool (`python -m app.cli capacity`).
- [ ] Reinstall a server and confirm it comes back with a fresh disk and a new
      root password.

## Troubleshooting

**VM boots with no network.** cloud-init did not consume the guestinfo payload.
Check `Change settings` privilege, cloud-init ≥ 21.3, the datasource config, and
`vmware-rpctool "info-get guestinfo.vendordata"` inside the guest.

**`Clone request rejected` immediately.** Usually a missing privilege or a name
collision. The driver marks `InvalidArgument`, `NotFound`, `DuplicateName`,
`InvalidState` and `InsufficientResources` as permanent, so it will not retry —
the error text in the jobs table is the vCenter fault message verbatim.

**Clone times out.** `HOSTPLANE_VSPHERE_TASK_TIMEOUT_SECONDS` defaults to 1800.
Thick-provisioned clones of large templates across slow storage can exceed it.

**Instances stuck in `provisioning`.** Check the worker is running and look at
`/admin/jobs`. `requeue_stuck_jobs` recovers jobs orphaned by a dead worker after
30 minutes.
