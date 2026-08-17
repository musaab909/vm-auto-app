# HostPlane

A VPS hosting control plane on VMware vSphere. Customers sign up, order a
fixed-price plan, and get a VM provisioned with a public IPv4 and SSH access,
managed from a web portal and a JSON API, billed monthly with automatic
suspension and termination for non-payment.

The product shape is Contabo-style: fixed plans, no metering, one monthly price.

## What this replaces

This repository previously held a ~70-line Flask prototype — one HTML form, a
ping-sweep IP picker, and a `deploy_vm()` that printed a line and returned
`True`. Three things it got wrong are worth naming, because the architecture here
is largely a response to them:

| Prototype | Now |
| --- | --- |
| `ip_scanner.py` pinged a /24 one host at a time inside the web request (up to ~254s) and treated "no ICMP reply" as "unallocated", so two simultaneous orders got the same address | DB-backed IPAM: one indexed `SELECT … FOR UPDATE SKIP LOCKED`, concurrency-safe by construction and covered by a test that races eight allocators |
| `vmware_lib.deploy_vm()` was a stub; `pyvmomi` was a dependency that was never imported | A real pyvmomi driver behind a `HypervisorDriver` protocol, with a faithful `MockDriver` for development and CI |
| Provisioning ran inline in the request; `app.py` never called `app.run()`, so the container exited immediately | Every hypervisor call runs in a Celery worker; the API only writes rows and enqueues |

## Architecture

Three runtime roles from one image — **api** (FastAPI + portal), **worker**
(Celery), **beat** (Celery scheduler) — over PostgreSQL and Redis.

```
Browser / API client
        │
        ▼
┌───────────────────┐        ┌────────────┐
│ api               │───────▶│ PostgreSQL │◀──────┐
│ FastAPI + portal  │        └────────────┘       │
└─────────┬─────────┘                             │
          │ enqueue after commit                  │
          ▼                                       │
   ┌────────────┐        ┌──────────────────┐     │
   │   Redis    │───────▶│ worker · beat    │─────┘
   └────────────┘        │ Celery           │
                         └─────────┬────────┘
                                   │ pyvmomi
                                   ▼
                            vCenter / ESXi
```

The API never talks to vSphere. A wedged vCenter can stall provisioning, but it
can never stall an HTTP response.

The codebase is synchronous throughout — pyvmomi blocks, so an async stack would
buy nothing while forcing two parallel session setups. FastAPI runs `def`
endpoints in its threadpool, and workers share the same models and session
factory.

### Layout

```
app/
  config.py          pydantic-settings; every secret from the environment
  db.py              engine and session factory, shared by API and workers
  security.py        argon2 hashing, token minting, signed cookies
  models/            SQLAlchemy models (26 tables)
  schemas.py         pydantic request/response models
  api/v1/            auth · catalog · instances · billing · webhooks · admin
  portal/            Jinja2 + HTMX customer and staff UI
  drivers/           HypervisorDriver protocol · MockDriver · VSphereDriver
  billing/           PaymentGateway protocol · FakeGateway · invoicing ·
                     subscriptions · dunning
  services/          ipam · provisioning · quota · auth · audit · dispatch
  workers/           Celery app, tasks and beat schedule
migrations/          Alembic
tests/               139 tests against real PostgreSQL
```

## Running it

```bash
cp .env.example .env          # then set HOSTPLANE_SECRET_KEY
docker compose up -d          # migrations run automatically before api starts
docker compose run --rm api python -m app.cli seed
docker compose run --rm api python -m app.cli create-admin \
    --email you@example.com --password 'a-strong-password'
```

The portal is on <http://localhost:8000>, API docs on
<http://localhost:8000/api/docs>, staff area on `/admin`.

The default driver is `mock`, so the whole platform runs end to end with no
hypervisor at all — order a server and watch it reach `running`. Switch
`HOSTPLANE_DRIVER=vsphere` once vCenter is reachable and templates are prepared
(see [docs/vcenter-setup.md](docs/vcenter-setup.md)).

### Without Docker

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
createdb hostplane
export HOSTPLANE_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/hostplane
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli seed
.venv/bin/uvicorn app.main:app --reload
# in other shells:
.venv/bin/celery -A app.workers.celery_app.celery_app worker -l info
.venv/bin/celery -A app.workers.celery_app.celery_app beat -l info
```

## How the pieces work

### IPAM

Addresses are materialised rows with an explicit lifecycle
(`available → assigned → quarantined → available`). Allocation takes one row
under `FOR UPDATE SKIP LOCKED`, so concurrent provisioners never collide and
never queue behind each other.

Released addresses are quarantined (default 24h) before reuse, so a new customer
does not inherit the previous tenant's traffic, DNS records or blocklist
reputation.

### Provisioning

Ordering happens in **one transaction**: instance row, subscription, IP
allocation and job row all commit together, or none of them do. The Celery task
is published from SQLAlchemy's `after_commit` hook (`services/dispatch.py`), so a
worker can never dequeue a job whose row is not yet visible — the bug you get for
free with FastAPI `BackgroundTasks`, which run before the request session
commits.

Workers retry with exponential backoff. `DriverError` carries a `permanent` flag
so a bad spec or missing template fails immediately instead of burning three
attempts. A failed provision that never produced a VM releases its IP rather than
holding pool capacity hostage.

A reconciler runs every 15 minutes, comparing recorded state against the
hypervisor. The database is authoritative for *intent*; vCenter is authoritative
for *fact*. It also adopts orphaned VMs — one created just before a worker died,
before the reference was saved — which would otherwise run forever, unbilled.

### Billing

All money is integer minor units with an explicit currency. No floating point
appears anywhere in the billing path. Invoice numbers come from a Postgres
sequence, never `COUNT(*)`.

The dunning ladder:

```
ACTIVE
  └─ payment fails ─────────→ PAST_DUE    (retries on day 1, 3, 7)
       └─ retries exhausted ─→ SUSPENDED  (powered off, disk preserved)
            └─ grace expires ─→ TERMINATED (deleted, IP released)
```

Suspension is deliberately reversible: paying late gets your data back.
Termination is the only destructive step and only happens after the full grace
window. Every retry reuses the *same* invoice, so a customer is never billed
several times for one month.

### Payment gateway

No live payment provider is wired. `billing/gateway.py` defines a
`PaymentGateway` protocol and `FakeGateway` implements it deterministically —
declining on demand, enforcing idempotency the way a real gateway does, and
emitting signed webhook events so the decline, retry and webhook paths are all
genuinely exercised in tests.

Test tokens: `tok_visa` succeeds, `tok_decline` and `tok_insufficient` decline,
`tok_action` requires 3-D Secure, `tok_error` simulates a transport failure.

Adding Stripe means writing one module that implements the protocol and changing
one line in `billing/__init__.py`. Nothing in invoicing or dunning moves.

## Security

- vCenter TLS verification is on by default; production config **refuses to boot**
  with it disabled, with a dev secret, or with debug on.
- Argon2 password hashing with transparent rehash on parameter changes.
- API tokens are stored as SHA-256 hashes; the plaintext is shown once.
- Generated root passwords are shown once, stored only as a hash, and cleared
  from the job payload after provisioning.
- Ownership is enforced in a dependency (`get_owned_instance`), not per route.
  Cross-tenant access returns 404, not 403 — a 403 would confirm the id exists.
- CSRF tokens on every portal form, bound to the session cookie.
- Rate limiting on auth, backed by Postgres rather than Redis so it cannot fail
  open when the broker is down.
- Webhooks are signature-verified, and `(gateway, external_id)` is unique so a
  redelivery is a no-op instead of a double credit.
- No card data ever reaches this service — only gateway tokens.

## Testing

```bash
pytest -q                      # 139 tests
ruff check . && mypy app
```

Tests run against **real PostgreSQL**, not SQLite: the allocator depends on
`FOR UPDATE SKIP LOCKED` and the schema uses JSONB. Testing the allocator against
a different engine would prove nothing about the property that matters.

Point `HOSTPLANE_TEST_DATABASE_URL` at a scratch database (default
`…/hostplane_test`); it is dropped and rebuilt per session.

Notable coverage:

- eight threads racing for five addresses — exactly five win, three get
  `NoCapacity`, no address is ever issued twice
- the full create → running → stop → start → delete lifecycle, with the IP
  returned to the pool
- the dunning ladder walked end to end: retry → suspend → terminate, and
  paying after suspension restoring service
- a worker retry after a lost commit charging the customer exactly once
- a replayed webhook being ignored
- cross-tenant access returning 404 on every instance route

## What is not verified here

Stated plainly, because it matters for anyone deploying this:

- **No real vCenter was reachable while this was built.** The vSphere driver is
  written against the pyvmomi API and unit-tested for cloud-init payload
  generation, inventory-lookup failures and fault classification — but no real
  `CloneVM_Task`, guest cloud-init run, or WebMKS console ticket has been
  executed. Validate against your own vSphere before trusting it in production;
  [docs/vcenter-setup.md](docs/vcenter-setup.md) lists the exact checks.
- **No live payment gateway.** Billing runs against `FakeGateway` by design.
- **Email is persisted, not sent.** There is no SMTP transport, so verification
  and dunning messages land in the `email_outbox` table rather than being
  silently dropped. A real sender can drain it.

## Operations

```bash
python -m app.cli seed                 # starter catalog, region and IP pool
python -m app.cli create-admin --email … --password …
python -m app.cli capacity             # IP capacity per region and pool
python -m app.cli run-billing          # run renewals and dunning now
python -m app.cli reconcile            # re-sync against the hypervisor
```

Scheduled work (Celery beat): renewals daily at 02:00 UTC, dunning at 03:00,
reconcile every 15 minutes, IP quarantine sweep hourly, stuck-job requeue every
10 minutes.

`/healthz` is liveness (no database touch); `/readyz` is readiness and checks the
database.
