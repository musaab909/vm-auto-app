"""Console session tests.

The property under test is that a console ticket is a *credential*: it must be
unforgeable, usable once, short-lived, and useless to anyone but the person it
was issued to. The `ConsoleTicket` model always had `single_use` and
`expires_at`; until now nothing read them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.drivers.mock import MockDriver
from app.models.compute import ConsoleTicket
from app.models.enums import InstanceStatus, JobType
from app.security import utcnow
from app.services import console as console_service
from app.services import provisioning
from app.services.errors import InvalidStateError, NotFoundError


def running_instance(db, account):
    instance, job, _ = provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-central",
        label="web-01",
    )
    provisioning.run_job(db, job, MockDriver())
    assert instance.status is InstanceStatus.RUNNING
    return instance


# --- minting ----------------------------------------------------------------


def test_mint_returns_a_handle_and_persists_a_ticket(db, catalog, account, user):
    instance = running_instance(db, account)

    handle, record = console_service.mint(
        db, instance=instance, user_id=user.id, driver=MockDriver()
    )

    assert handle and handle != record.ticket, "the handle must not be the ticket"
    assert record.ticket not in handle
    assert record.single_use is True
    assert record.used_at is None
    assert record.expires_at > utcnow()


def test_mint_is_refused_unless_the_server_is_running(db, catalog, account, user):
    instance = running_instance(db, account)
    driver = MockDriver()

    stop = provisioning.request_action(db, instance=instance, job_type=JobType.STOP)
    provisioning.run_job(db, stop, driver)

    with pytest.raises(InvalidStateError, match="only available while the server is running"):
        console_service.mint(db, instance=instance, user_id=user.id, driver=driver)


def test_mint_writes_an_audit_entry(db, catalog, account, user):
    from app.models.identity import AuditLog

    instance = running_instance(db, account)
    console_service.mint(db, instance=instance, user_id=user.id, driver=MockDriver())

    entry = (
        db.execute(select(AuditLog).where(AuditLog.action == "instance.console_opened"))
        .scalars()
        .first()
    )
    assert entry is not None
    assert entry.target_id == instance.id


# --- redemption -------------------------------------------------------------


def test_redeem_returns_connection_details(db, catalog, account, user):
    instance = running_instance(db, account)
    handle, record = console_service.mint(
        db, instance=instance, user_id=user.id, driver=MockDriver()
    )

    details = console_service.redeem(db, handle, user_id=user.id)

    assert details.instance_id == instance.id
    assert details.ticket == record.ticket
    assert details.host == record.host


def test_a_handle_can_only_be_redeemed_once(db, catalog, account, user):
    """The whole point: a leaked link is spent, not reusable."""
    instance = running_instance(db, account)
    handle, _ = console_service.mint(db, instance=instance, user_id=user.id, driver=MockDriver())

    console_service.redeem(db, handle, user_id=user.id)

    with pytest.raises(InvalidStateError, match="already been used"):
        console_service.redeem(db, handle, user_id=user.id)


def test_redemption_marks_the_ticket_used(db, catalog, account, user):
    instance = running_instance(db, account)
    handle, record = console_service.mint(
        db, instance=instance, user_id=user.id, driver=MockDriver()
    )

    console_service.redeem(db, handle, user_id=user.id)

    assert record.used_at is not None


def test_an_expired_handle_is_rejected(db, catalog, account, user):
    instance = running_instance(db, account)
    handle, record = console_service.mint(
        db, instance=instance, user_id=user.id, driver=MockDriver()
    )

    record.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()

    with pytest.raises(InvalidStateError, match="expired"):
        console_service.redeem(db, handle, user_id=user.id)


def test_a_forged_handle_is_rejected(db, catalog, account, user):
    with pytest.raises(NotFoundError):
        console_service.redeem(db, "not-a-real-handle", user_id=user.id)


def test_a_handle_issued_to_another_user_is_rejected(db, catalog, account, user):
    """Even inside one account: the audit log names a person, not an org."""
    instance = running_instance(db, account)
    handle, _ = console_service.mint(db, instance=instance, user_id=user.id, driver=MockDriver())

    with pytest.raises(NotFoundError):
        console_service.redeem(db, handle, user_id="some-other-user-id")


# --- housekeeping -----------------------------------------------------------


def test_purge_removes_only_long_expired_tickets(db, catalog, account, user):
    instance = running_instance(db, account)
    _, fresh = console_service.mint(db, instance=instance, user_id=user.id, driver=MockDriver())
    _, stale = console_service.mint(db, instance=instance, user_id=user.id, driver=MockDriver())
    stale.expires_at = utcnow() - timedelta(days=2)
    db.flush()

    assert console_service.purge_expired(db) == 1

    remaining = db.execute(select(ConsoleTicket)).scalars().all()
    assert [t.id for t in remaining] == [fresh.id]


# --- API surface ------------------------------------------------------------


def test_api_mint_does_not_leak_the_ticket(client, catalog, db):
    """The JSON body must carry no credential — bodies end up in logs."""
    db.commit()
    client.post(
        "/api/v1/auth/register",
        json={"email": "console@example.com", "password": "correct-horse-1"},
    )
    created = client.post(
        "/api/v1/instances",
        json={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
        },
    ).json()
    instance_id = created["instance"]["id"]

    response = client.post(f"/api/v1/instances/{instance_id}/console")
    assert response.status_code == 200

    body = response.json()
    assert "handle" in body and "redeem_url" in body
    assert "ticket" not in body

    db.expire_all()
    record = db.execute(select(ConsoleTicket)).scalars().first()
    assert record.ticket not in response.text


def test_api_redeem_works_once_then_409s(client, catalog, db):
    db.commit()
    client.post(
        "/api/v1/auth/register",
        json={"email": "console2@example.com", "password": "correct-horse-1"},
    )
    created = client.post(
        "/api/v1/instances",
        json={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
        },
    ).json()
    instance_id = created["instance"]["id"]

    handle = client.post(f"/api/v1/instances/{instance_id}/console").json()["handle"]

    first = client.get(f"/api/v1/instances/console/{handle}")
    assert first.status_code == 200
    assert first.json()["instance_id"] == instance_id

    second = client.get(f"/api/v1/instances/console/{handle}")
    assert second.status_code == 409


def test_portal_console_page_renders(client, catalog, db):
    import re

    db.commit()
    signup = client.get("/signup")
    token = re.search(r'name="csrf_token" value="([^"]+)"', signup.text).group(1)
    client.post(
        "/signup",
        data={
            "email": "console3@example.com",
            "password": "correct-horse-1",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    order_page = client.get("/order")
    token = re.search(r'name="csrf_token" value="([^"]+)"', order_page.text).group(1)
    redirect = client.post(
        "/order",
        data={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    instance_id = redirect.headers["location"].split("/instances/")[1].split("?")[0]

    # The button is offered on the instance page once the server is running.
    detail = client.get(f"/instances/{instance_id}")
    assert f"/instances/{instance_id}/console" in detail.text

    page = client.get(f"/instances/{instance_id}/console")
    assert page.status_code == 200
    assert "Console" in page.text
