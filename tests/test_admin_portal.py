"""Staff portal tests: access control and rendering."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.models.enums import UserRole
from app.models.identity import User


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "the form is missing its CSRF token"
    return match.group(1)


@pytest.fixture
def customer(client, catalog, db):
    db.commit()
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "cust@example.com", "password": "correct-horse-1"},
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def staff(client, customer, db):
    user = db.execute(select(User).where(User.email == "cust@example.com")).scalar_one()
    user.role = UserRole.ADMIN
    db.commit()
    return user


def test_admin_pages_are_closed_to_customers(client, customer):
    for path in (
        "/admin",
        "/admin/instances",
        "/admin/jobs",
        "/admin/accounts",
        "/admin/invoices",
        "/admin/catalog",
        "/admin/pools",
    ):
        assert client.get(path).status_code == 403, path


def test_admin_pages_are_closed_to_anonymous_visitors(client, catalog, db):
    db.commit()
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_staff_can_open_every_admin_page(client, staff):
    for path in (
        "/admin",
        "/admin/instances",
        "/admin/jobs",
        "/admin/accounts",
        "/admin/invoices",
        "/admin/catalog",
        "/admin/pools",
    ):
        assert client.get(path).status_code == 200, path


def test_overview_shows_ip_capacity(client, staff):
    page = client.get("/admin")
    assert "IP capacity" in page.text
    assert "198.51.100.0/29" in page.text


def test_withdrawing_a_plan_stops_new_orders(client, staff, db):
    from app.models.catalog import Plan

    catalog_page = client.get("/admin/catalog")
    plan = db.execute(select(Plan).where(Plan.slug == "vps-s")).scalar_one()

    response = client.post(
        f"/admin/catalog/plans/{plan.id}/toggle",
        data={"csrf_token": csrf_from(catalog_page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    assert db.execute(select(Plan).where(Plan.slug == "vps-s")).scalar_one().is_active is False

    # The plan disappears from the public catalog and can no longer be ordered.
    assert client.get("/api/v1/plans").json() == []
    order = client.post(
        "/api/v1/instances",
        json={
            "label": "web-01",
            "plan": "vps-s",
            "image": "ubuntu-24-04",
            "region": "eu-central",
        },
    )
    assert order.status_code == 404


def test_creating_a_pool_materialises_its_addresses(client, staff, db, region):
    from app.services import ipam

    page = client.get("/admin/pools")
    response = client.post(
        "/admin/pools",
        data={
            "region_id": region.id,
            "name": "second pool",
            "cidr": "192.0.2.0/29",
            "gateway": "192.0.2.1",
            "vsphere_network": "VM Network",
            "vlan_id": "200",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    # The original /29 has 5 free plus 5 from the new pool; both gateways reserved.
    assert ipam.region_available_count(db, region.id) == 10


def test_creating_a_pool_rejects_a_bad_cidr(client, staff, region):
    page = client.get("/admin/pools")
    response = client.post(
        "/admin/pools",
        data={
            "region_id": region.id,
            "name": "bad",
            "cidr": "not-a-network",
            "csrf_token": csrf_from(page.text),
        },
    )
    assert response.status_code == 422


def test_admin_form_requires_csrf(client, staff, region):
    response = client.post(
        "/admin/pools",
        data={"region_id": region.id, "name": "x", "cidr": "192.0.2.0/29"},
    )
    assert response.status_code == 403
