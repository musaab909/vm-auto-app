"""End-to-end API tests through the real ASGI stack.

Celery runs eagerly (`task_always_eager`), so a `POST /instances` walks the whole
path — order, IPAM, job, mock driver — exactly as it would in production, minus
the broker.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.compute import Instance
from app.models.enums import InstanceStatus
from app.security import sign_webhook


@pytest.fixture
def signed_up(client, catalog, db):
    """A registered, logged-in customer. The cookie lives on the client."""
    db.commit()  # make fixture rows visible to the app's own session
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "api-user@example.com", "password": "correct-horse-1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- auth -------------------------------------------------------------------


def test_register_then_fetch_me(client, signed_up):
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "api-user@example.com"


def test_register_rejects_a_weak_password(client, db):
    db.commit()
    response = client.post(
        "/api/v1/auth/register", json={"email": "weak@example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_register_rejects_a_duplicate_email(client, signed_up, db):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "api-user@example.com", "password": "correct-horse-1"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_login_with_a_bad_password_is_rejected(client, signed_up):
    client.post("/api/v1/auth/logout")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "api-user@example.com", "password": "wrong-password-here"},
    )
    assert response.status_code == 401


def test_login_error_does_not_reveal_whether_the_email_exists(client, db):
    db.commit()
    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "some-password-1"},
    )
    assert unknown.status_code == 401
    assert "incorrect" in unknown.json()["error"].lower()


def test_unauthenticated_api_access_is_401(client, db):
    db.commit()
    assert client.get("/api/v1/instances").status_code == 401


def test_api_token_authenticates_requests(client, signed_up):
    created = client.post("/api/v1/auth/tokens", json={"name": "ci"})
    assert created.status_code == 201
    plaintext = created.json()["plaintext"]
    assert plaintext.startswith("hp_")

    client.cookies.clear()  # prove the token alone is sufficient
    response = client.get("/api/v1/instances", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200


def test_revoked_api_token_stops_working(client, signed_up):
    created = client.post("/api/v1/auth/tokens", json={"name": "ci"}).json()
    plaintext, token_id = created["plaintext"], created["token"]["id"]

    assert client.delete(f"/api/v1/auth/tokens/{token_id}").status_code == 204

    client.cookies.clear()
    response = client.get("/api/v1/instances", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 401


# --- catalog ----------------------------------------------------------------


def test_catalog_is_public(client, catalog, db):
    db.commit()
    assert client.get("/api/v1/plans").status_code == 200
    assert len(client.get("/api/v1/plans").json()) >= 1
    assert len(client.get("/api/v1/regions").json()) >= 1
    assert len(client.get("/api/v1/images").json()) >= 1


def test_ssh_key_roundtrip_and_fingerprint(client, signed_up):
    # A real ed25519 public key body (32-byte key), so the fingerprint is genuine.
    key = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ8f1Q0m0gk5xg6q6WQ7Yc9L6cQ0m0gk5xg6q6WQ7Yc9 "
        "user@laptop"
    )
    created = client.post("/api/v1/ssh-keys", json={"name": "laptop", "public_key": key})
    assert created.status_code == 201, created.text
    assert created.json()["fingerprint"].startswith("SHA256:")

    duplicate = client.post("/api/v1/ssh-keys", json={"name": "again", "public_key": key})
    assert duplicate.status_code == 409


def test_malformed_ssh_key_is_rejected(client, signed_up):
    response = client.post(
        "/api/v1/ssh-keys", json={"name": "bad", "public_key": "ssh-ed25519 not-base64!!"}
    )
    assert response.status_code == 422


# --- instances --------------------------------------------------------------


def order_payload(**overrides):
    payload = {
        "label": "web-01",
        "plan": "vps-s",
        "image": "ubuntu-24-04",
        "region": "eu-central",
    }
    payload.update(overrides)
    return payload


def test_create_instance_runs_the_whole_pipeline(client, signed_up, db):
    response = client.post("/api/v1/instances", json=order_payload())
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["root_password"], "the password is returned exactly once"
    assert body["instance"]["label"] == "web-01"

    # Eager Celery ran the provisioning job inline.
    instance = db.get(Instance, body["instance"]["id"])
    db.refresh(instance)
    assert instance.status is InstanceStatus.RUNNING
    assert instance.hypervisor_ref is not None
    assert instance.primary_ip is not None


def test_instance_appears_in_the_list(client, signed_up):
    client.post("/api/v1/instances", json=order_payload())
    listing = client.get("/api/v1/instances")
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_power_actions_move_the_instance(client, signed_up):
    created = client.post("/api/v1/instances", json=order_payload()).json()
    instance_id = created["instance"]["id"]

    stop = client.post(f"/api/v1/instances/{instance_id}/stop", json={"hard": False})
    assert stop.status_code == 202

    detail = client.get(f"/api/v1/instances/{instance_id}").json()
    assert detail["status"] == "stopped"

    start = client.post(f"/api/v1/instances/{instance_id}/start")
    assert start.status_code == 202
    assert client.get(f"/api/v1/instances/{instance_id}").json()["status"] == "running"


def test_starting_a_running_instance_is_409(client, signed_up):
    created = client.post("/api/v1/instances", json=order_payload()).json()
    instance_id = created["instance"]["id"]

    response = client.post(f"/api/v1/instances/{instance_id}/start")
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"


def test_rebuild_requires_explicit_confirmation(client, signed_up):
    created = client.post("/api/v1/instances", json=order_payload()).json()
    instance_id = created["instance"]["id"]

    unconfirmed = client.post(f"/api/v1/instances/{instance_id}/rebuild", json={"confirm": False})
    assert unconfirmed.status_code == 422

    confirmed = client.post(f"/api/v1/instances/{instance_id}/rebuild", json={"confirm": True})
    assert confirmed.status_code == 202


def test_resize_to_a_smaller_disk_is_rejected(client, signed_up, db, bigger_plan):
    db.commit()
    created = client.post("/api/v1/instances", json=order_payload(plan="vps-m")).json()
    instance_id = created["instance"]["id"]

    client.post(f"/api/v1/instances/{instance_id}/stop", json={"hard": False})
    response = client.post(f"/api/v1/instances/{instance_id}/resize", json={"plan": "vps-s"})

    assert response.status_code == 422
    assert "shrunk" in response.json()["error"]


def test_delete_releases_the_address(client, signed_up, db, catalog):
    from app.services import ipam

    created = client.post("/api/v1/instances", json=order_payload()).json()
    instance_id = created["instance"]["id"]

    assert client.delete(f"/api/v1/instances/{instance_id}").status_code == 202

    db.expire_all()
    assert ipam.region_available_count(db, catalog["region"].id) == 5


def test_deleted_instances_leave_the_listing(client, signed_up):
    created = client.post("/api/v1/instances", json=order_payload()).json()
    client.delete(f"/api/v1/instances/{created['instance']['id']}")
    assert client.get("/api/v1/instances").json() == []


def test_jobs_are_visible_for_an_instance(client, signed_up):
    created = client.post("/api/v1/instances", json=order_payload()).json()
    jobs = client.get(f"/api/v1/instances/{created['instance']['id']}/jobs")
    assert jobs.status_code == 200
    assert jobs.json()[0]["type"] == "provision"
    assert jobs.json()[0]["status"] == "succeeded"


# --- authorization ----------------------------------------------------------


def test_one_customer_cannot_touch_another_customers_instance(client, signed_up, db):
    """Cross-tenant access must 404, not 403 — a 403 would confirm the id exists."""
    created = client.post("/api/v1/instances", json=order_payload()).json()
    victim_id = created["instance"]["id"]

    client.post("/api/v1/auth/logout")
    client.cookies.clear()
    attacker = client.post(
        "/api/v1/auth/register",
        json={"email": "attacker@example.com", "password": "correct-horse-2"},
    )
    assert attacker.status_code == 201

    assert client.get(f"/api/v1/instances/{victim_id}").status_code == 404
    assert client.post(f"/api/v1/instances/{victim_id}/stop", json={}).status_code == 404
    assert client.delete(f"/api/v1/instances/{victim_id}").status_code == 404
    assert client.get("/api/v1/instances").json() == []


def test_admin_routes_reject_ordinary_customers(client, signed_up):
    assert client.get("/api/v1/admin/stats").status_code == 403
    assert client.get("/api/v1/admin/instances").status_code == 403


def test_staff_can_read_platform_stats(client, signed_up, db):
    from app.models.enums import UserRole
    from app.models.identity import User

    user = db.execute(select(User).where(User.email == "api-user@example.com")).scalar_one()
    user.role = UserRole.ADMIN
    db.commit()

    stats = client.get("/api/v1/admin/stats")
    assert stats.status_code == 200
    assert "instances_total" in stats.json()

    capacity = client.get("/api/v1/admin/capacity")
    assert capacity.status_code == 200
    assert capacity.json()[0]["available"] == 5


# --- webhooks ---------------------------------------------------------------


def post_webhook(client, body: dict, *, signature: str | None = None):
    raw = json.dumps(body).encode()
    sig = signature if signature is not None else sign_webhook(raw, "dev-webhook-secret")
    return client.post(
        "/api/v1/webhooks/payments",
        content=raw,
        headers={"X-Signature": sig, "Content-Type": "application/json"},
    )


def test_webhook_rejects_a_bad_signature(client, db):
    db.commit()
    response = post_webhook(client, {"id": "evt_1", "type": "payment.failed"}, signature="nope")
    assert response.status_code == 422


def test_webhook_is_accepted_when_signed(client, db):
    db.commit()
    response = post_webhook(client, {"id": "evt_1", "type": "payment.failed", "data": {}})
    assert response.status_code == 200
    assert response.json()["status"] == "processed"


def test_replayed_webhook_is_ignored(client, db):
    """Gateways retry aggressively; a redelivery must never apply twice."""
    db.commit()
    body = {"id": "evt_replay", "type": "payment.failed", "data": {}}

    first = post_webhook(client, body)
    second = post_webhook(client, body)

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate"


def test_webhook_settles_a_pending_payment(client, signed_up, db):
    """A success notification arriving out of band pays the invoice exactly once."""
    from app.billing import invoicing
    from app.models.billing import Invoice, Payment
    from app.models.enums import InvoiceStatus, PaymentStatus
    from app.models.identity import Account, User

    user = db.execute(select(User).where(User.email == "api-user@example.com")).scalar_one()
    account = db.execute(select(Account).where(Account.owner_user_id == user.id)).scalar_one()

    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )
    payment = Payment(
        account_id=account.id,
        invoice_id=invoice.id,
        gateway="fake",
        idempotency_key="inv_test_att_1",
        amount_cents=599,
        currency="EUR",
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.commit()

    body = {
        "id": "evt_paid",
        "type": "payment.succeeded",
        "data": {
            "idempotency_key": "inv_test_att_1",
            "charge_ref": "ch_abc",
            "amount_cents": 599,
        },
    }
    assert post_webhook(client, body).json()["handled"] is True

    db.expire_all()
    settled = db.get(Invoice, invoice.id)
    assert settled.status is InvoiceStatus.PAID
    assert settled.amount_paid_cents == 599

    # A redelivery must not credit the account a second time.
    post_webhook(client, body)
    db.expire_all()
    assert db.get(Invoice, invoice.id).amount_paid_cents == 599


# --- health -----------------------------------------------------------------


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"


def test_security_headers_are_present(client):
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-request-id"]
