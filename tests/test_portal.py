"""Customer portal tests: rendering, CSRF, and the order flow."""

from __future__ import annotations

import re

import pytest

from app.models.enums import InstanceStatus


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "the form is missing its CSRF token"
    return match.group(1)


@pytest.fixture
def portal_user(client, catalog, db):
    """A signed-up customer with a session cookie, via the portal forms."""
    db.commit()
    page = client.get("/signup")
    token = csrf_from(page.text)
    response = client.post(
        "/signup",
        data={
            "email": "portal@example.com",
            "password": "correct-horse-1",
            "full_name": "Portal User",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


# --- public pages -----------------------------------------------------------


def test_home_lists_plans(client, catalog, db):
    db.commit()
    response = client.get("/")
    assert response.status_code == 200
    assert "VPS S" in response.text
    assert "5.99 EUR" in response.text


def test_login_and_signup_pages_render(client):
    assert client.get("/login").status_code == 200
    assert client.get("/signup").status_code == 200


def test_signed_in_home_redirects_to_the_dashboard(client, portal_user):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


# --- CSRF -------------------------------------------------------------------


def test_form_post_without_a_csrf_token_is_rejected(client, catalog, db):
    db.commit()
    response = client.post(
        "/signup", data={"email": "x@example.com", "password": "correct-horse-1"}
    )
    assert response.status_code == 403
    assert "CSRF" in response.text


def test_form_post_with_a_forged_csrf_token_is_rejected(client, catalog, db):
    db.commit()
    response = client.post(
        "/signup",
        data={
            "email": "x@example.com",
            "password": "correct-horse-1",
            "csrf_token": "forged-token",
        },
    )
    assert response.status_code == 403


# --- authenticated pages ----------------------------------------------------


def test_dashboard_requires_a_session(client, db):
    db.commit()
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_dashboard_shows_the_empty_state(client, portal_user):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "No servers yet" in response.text


def test_order_page_lists_plans_images_and_regions(client, portal_user):
    response = client.get("/order")
    assert response.status_code == 200
    assert "VPS S" in response.text
    assert "Ubuntu 24.04" in response.text
    assert "Frankfurt" in response.text


def test_ordering_provisions_and_shows_the_password_once(client, portal_user, db):
    from app.models.compute import Instance

    page = client.get("/order")
    response = client.post(
        "/order",
        data={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
            "hostname": "web-01",
            "ssh_key_id": "",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.endswith("?new=1")

    detail = client.get(location)
    assert detail.status_code == 200
    assert "Your root password" in detail.text
    assert "only time it will be shown" in detail.text

    # A reload must not show the password again.
    instance_id = location.split("/instances/")[1].split("?")[0]
    again = client.get(f"/instances/{instance_id}")
    assert "Your root password" not in again.text

    db.expire_all()
    instance = db.get(Instance, instance_id)
    assert instance.status is InstanceStatus.RUNNING


def test_dashboard_lists_a_provisioned_server(client, portal_user):
    page = client.get("/order")
    client.post(
        "/order",
        data={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )

    dashboard = client.get("/dashboard")
    assert "web-01" in dashboard.text
    assert "running" in dashboard.text


def test_power_action_through_the_portal(client, portal_user):
    page = client.get("/order")
    redirect = client.post(
        "/order",
        data={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    instance_id = redirect.headers["location"].split("/instances/")[1].split("?")[0]

    detail = client.get(f"/instances/{instance_id}")
    response = client.post(
        f"/instances/{instance_id}/action",
        data={"action": "stop", "csrf_token": csrf_from(detail.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert "stopped" in client.get(f"/instances/{instance_id}").text


def test_status_fragment_renders_for_htmx(client, portal_user):
    page = client.get("/order")
    redirect = client.post(
        "/order",
        data={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    instance_id = redirect.headers["location"].split("/instances/")[1].split("?")[0]

    fragment = client.get(f"/instances/{instance_id}/status", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert 'id="status-card"' in fragment.text
    # A settled instance must stop polling, or the browser hammers the server.
    assert "hx-trigger" not in fragment.text


def test_billing_and_settings_pages_render(client, portal_user):
    assert client.get("/billing").status_code == 200
    assert client.get("/settings").status_code == 200


def test_adding_a_card_through_the_portal(client, portal_user):
    page = client.get("/billing")
    response = client.post(
        "/billing/payment-methods",
        data={"token": "tok_visa", "csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    billing = client.get("/billing")
    assert "4242" in billing.text


def test_ssh_key_can_be_added_and_removed(client, portal_user):
    page = client.get("/settings")
    key = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ8f1Q0m0gk5xg6q6WQ7Yc9L6cQ0m0gk5xg6q6WQ7Yc9 "
        "user@laptop"
    )
    client.post(
        "/settings/ssh-keys",
        data={"name": "laptop", "public_key": key, "csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )

    settings_page = client.get("/settings")
    assert "laptop" in settings_page.text
    assert "SHA256:" in settings_page.text


def test_one_customer_cannot_open_another_customers_instance_page(client, portal_user, db):
    page = client.get("/order")
    redirect = client.post(
        "/order",
        data={
            "label": "victim",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    victim_id = redirect.headers["location"].split("/instances/")[1].split("?")[0]

    client.get("/logout")
    client.cookies.clear()
    signup = client.get("/signup")
    client.post(
        "/signup",
        data={
            "email": "attacker@example.com",
            "password": "correct-horse-2",
            "csrf_token": csrf_from(signup.text),
        },
        follow_redirects=False,
    )

    response = client.get(f"/instances/{victim_id}")
    assert response.status_code == 404


def test_login_next_parameter_cannot_redirect_off_site(client, portal_user):
    """An open redirect after login would be a phishing primitive."""
    client.get("/logout")
    client.cookies.clear()

    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": "portal@example.com",
            "password": "correct-horse-1",
            "next": "https://evil.example.com/steal",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
