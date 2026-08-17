"""Test fixtures.

Tests run against a real PostgreSQL database, not SQLite: the IPAM allocator
depends on `FOR UPDATE SKIP LOCKED` and the schema uses JSONB, neither of which
SQLite can emulate. Testing the allocator against a different engine than
production would prove nothing about the property that matters.

Point `HOSTPLANE_TEST_DATABASE_URL` at a scratch database; it is dropped and
recreated per session.
"""

from __future__ import annotations

import os

import pytest

TEST_DB_URL = os.environ.get(
    "HOSTPLANE_TEST_DATABASE_URL",
    "postgresql+psycopg://hostplane:hostplane@127.0.0.1:5432/hostplane_test",
)

# Must be set before any application module reads settings, since `app.db` builds
# its engine at import time.
os.environ["HOSTPLANE_DATABASE_URL"] = TEST_DB_URL
os.environ["HOSTPLANE_ENV"] = "test"
os.environ["HOSTPLANE_DRIVER"] = "mock"
os.environ["HOSTPLANE_TASK_ALWAYS_EAGER"] = "true"
os.environ.setdefault("HOSTPLANE_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("HOSTPLANE_IP_QUARANTINE_HOURS", "0")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.billing import fake_gateway  # noqa: E402
from app.billing.fake_gateway import FakeGateway  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.drivers.mock import MockDriver  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.catalog import OsImage, Plan, PlanRegion, Region  # noqa: E402
from app.models.enums import IPVersion  # noqa: E402
from app.models.network import IpPool  # noqa: E402
from app.services import auth as auth_service  # noqa: E402
from app.services import ipam  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    """Build the schema once for the whole session."""
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()

    Base.metadata.create_all(engine)

    # Created by the Alembic migration in real deployments; `create_all` does not
    # know about it, so tests that mint invoice numbers need it here.
    with engine.connect() as conn:
        conn.execute(text("CREATE SEQUENCE IF NOT EXISTS invoice_number_seq START WITH 1"))
        conn.commit()

    yield

    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Truncate all tables and reset in-memory doubles between tests."""
    MockDriver.reset()
    FakeGateway.reset()
    fake_gateway.FakeGateway.force_outcome = None

    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER SEQUENCE invoice_number_seq RESTART WITH 1"))
        conn.commit()
    yield


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def region(db: Session) -> Region:
    region = Region(
        slug="eu-central",
        name="Frankfurt",
        country="DE",
        vsphere_datacenter="DC1",
        vsphere_cluster="Cluster-01",
        vsphere_datastore="ds1",
        vsphere_network="VM Network",
    )
    db.add(region)
    db.flush()
    return region


@pytest.fixture
def pool(db: Session, region: Region) -> IpPool:
    """A deliberately tiny pool so exhaustion is easy to test."""
    pool = IpPool(
        region_id=region.id,
        name="test pool",
        cidr="198.51.100.0/29",
        version=IPVersion.V4,
        gateway="198.51.100.1",
        prefix_length=29,
        vsphere_network="VM Network",
    )
    db.add(pool)
    db.flush()
    ipam.expand_pool(db, pool)
    return pool


@pytest.fixture
def plan(db: Session, region: Region) -> Plan:
    plan = Plan(
        slug="vps-s",
        name="VPS S",
        vcpu=2,
        ram_mb=4096,
        disk_gb=50,
        price_cents=599,
        currency="EUR",
    )
    db.add(plan)
    db.flush()
    db.add(PlanRegion(plan_id=plan.id, region_id=region.id))
    db.flush()
    return plan


@pytest.fixture
def bigger_plan(db: Session, region: Region) -> Plan:
    plan = Plan(
        slug="vps-m",
        name="VPS M",
        vcpu=4,
        ram_mb=8192,
        disk_gb=100,
        price_cents=1099,
        currency="EUR",
        sort_order=2,
    )
    db.add(plan)
    db.flush()
    db.add(PlanRegion(plan_id=plan.id, region_id=region.id))
    db.flush()
    return plan


@pytest.fixture
def image(db: Session) -> OsImage:
    image = OsImage(
        slug="ubuntu-24-04",
        name="Ubuntu 24.04",
        family="ubuntu",
        version="24.04",
        vsphere_template="tmpl-ubuntu-24.04",
        min_disk_gb=10,
    )
    db.add(image)
    db.flush()
    return image


@pytest.fixture
def account(db: Session):
    _user, account = auth_service.register_user(
        db, email="customer@example.com", password="correct-horse-1", full_name="Test Customer"
    )
    db.flush()
    return account


@pytest.fixture
def user(db: Session, account):
    return db.get(type(account).owner.property.mapper.class_, account.owner_user_id)


@pytest.fixture
def catalog(region, pool, plan, image):
    """Everything needed to place an order."""
    return {"region": region, "pool": pool, "plan": plan, "image": image}


@pytest.fixture
def client():
    """TestClient sharing the same engine as the `db` fixture."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_instance(db: Session, account, plan, image, region):
    """Create a persisted instance row.

    IPAM binds addresses to instances by foreign key, so tests that allocate need
    a real instance rather than a made-up id.
    """
    from app.models.compute import Instance

    created: list[Instance] = []

    def _make(label: str = "test-vm", **overrides) -> Instance:
        instance = Instance(
            account_id=account.id,
            plan_id=plan.id,
            image_id=image.id,
            region_id=region.id,
            label=label,
            hostname=overrides.pop("hostname", label),
            vcpu=plan.vcpu,
            ram_mb=plan.ram_mb,
            disk_gb=plan.disk_gb,
            **overrides,
        )
        db.add(instance)
        db.flush()
        created.append(instance)
        return instance

    return _make
