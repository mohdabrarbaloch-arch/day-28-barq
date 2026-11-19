"""Shared test fixtures.

Each test gets its own SQLite file in a temp directory, so tests never see each
other's rows and the suite can run in any order.

The first account registered on a fresh database becomes an admin; every later
one is a viewer. That is the real bootstrap rule in ``app/api/auth.py``, so the
fixtures lean on it rather than inventing a back door.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

# The settings object is cached at import time, so the environment has to be set
# before anything under app.* is imported.
_TMP = tempfile.mkdtemp(prefix="barq-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ["APP_ENV"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-in-production"
os.environ["BCRYPT_ROUNDS"] = "4"  # keep hashing fast in tests
os.environ["RATE_LIMIT_REQUESTS"] = "10000"
os.environ["AUTH_RATE_LIMIT_REQUESTS"] = "10000"


@pytest.fixture(scope="session")
def app_module():
    from app.main import app

    return app


@pytest.fixture(autouse=True)
def clean_database(app_module):
    """Wipe every table before each test.

    The engine is created once at import, so all tests share one SQLite file.
    Without this, "the first account becomes admin" is only true for whichever
    test happens to run first, and every later test silently gets a viewer -
    which is exactly the kind of order-dependent failure that makes a suite
    untrustworthy.
    """
    from app.core.database import Base, engine, init_db

    init_db()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client(app_module):
    """A TestClient with the lifespan run, so tables exist.

    Using TestClient without the context manager skips startup and every query
    fails with 'no such table'. That is a real trap and the reason this fixture
    exists rather than each test constructing its own client.
    """
    from fastapi.testclient import TestClient

    with TestClient(app_module) as test_client:
        yield test_client


@pytest.fixture()
def db_session():
    from app.core.database import SessionLocal, init_db

    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _register(client, prefix: str) -> tuple[dict, dict]:
    """Register a user and return (headers, user payload)."""
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@barq-example.com"
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "full_name": f"Test {prefix.title()}",
            "password": "test-password-123",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user"]


@pytest.fixture()
def admin_headers(client):
    """The first account on a fresh database is the admin."""
    headers, _ = _register(client, "admin")
    return headers


@pytest.fixture()
def admin_user(client, admin_headers):
    response = client.get("/api/auth/me", headers=admin_headers)
    assert response.status_code == 200
    return response.json()


@pytest.fixture()
def operator_headers(client, admin_headers):
    """A second account, promoted to operator by the admin.

    Registration alone yields a viewer, so this exercises the real promotion
    path instead of assuming a role.
    """
    headers, user = _register(client, "operator")
    promoted = client.patch(
        f"/api/auth/users/{user['id']}/role",
        json={"role": "operator"},
        headers=admin_headers,
    )
    assert promoted.status_code == 200, promoted.text
    return headers


@pytest.fixture()
def operator_user(client, operator_headers):
    response = client.get("/api/auth/me", headers=operator_headers)
    assert response.status_code == 200
    return response.json()


@pytest.fixture()
def viewer_headers(client, admin_headers):
    """A plain viewer: can read, cannot write."""
    headers, _ = _register(client, "viewer")
    return headers


@pytest.fixture()
def feeder(client, admin_headers):
    """A single feeder with no assets."""
    response = client.post(
        "/api/feeders",
        json={
            "code": f"FDR-{uuid.uuid4().hex[:6].upper()}",
            "name": "Test Feeder",
            "region": "Karachi",
            "peak_load_mw": 900.0,
            "industrial_share": 0.5,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def asset(client, admin_headers, feeder):
    """One thermal unit on the feeder fixture."""
    response = client.post(
        f"/api/feeders/{feeder['id']}/assets",
        json={
            "code": f"COAL-{uuid.uuid4().hex[:4].upper()}",
            "name": "Coal Unit",
            "kind": "thermal",
            "capacity_mw": 400.0,
            "min_stable_mw": 160.0,
            "ramp_mw_per_min": 6.0,
            "marginal_cost": 9800.0,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def seeded_grid(client, admin_headers):
    """A feeder with a realistic six-unit fleet and one telemetry reading."""
    feeder_response = client.post(
        "/api/feeders",
        json={
            "code": f"TST-{uuid.uuid4().hex[:6].upper()}",
            "name": "Seeded Feeder",
            "region": "Karachi",
            "peak_load_mw": 900.0,
            "industrial_share": 0.5,
        },
        headers=admin_headers,
    )
    assert feeder_response.status_code == 201, feeder_response.text
    feeder_id = feeder_response.json()["id"]

    suffix = uuid.uuid4().hex[:4].upper()
    assets = [
        {
            "code": f"COAL-{suffix}",
            "name": "Coal",
            "kind": "thermal",
            "capacity_mw": 400.0,
            "min_stable_mw": 160.0,
            "ramp_mw_per_min": 6.0,
            "marginal_cost": 9800.0,
        },
        {
            "code": f"GAS-{suffix}",
            "name": "Gas",
            "kind": "thermal",
            "capacity_mw": 220.0,
            "min_stable_mw": 80.0,
            "ramp_mw_per_min": 12.0,
            "marginal_cost": 14500.0,
        },
        {
            "code": f"HYD-{suffix}",
            "name": "Hydro",
            "kind": "hydro",
            "capacity_mw": 180.0,
            "min_stable_mw": 0.0,
            "ramp_mw_per_min": 45.0,
            "marginal_cost": 3200.0,
        },
        {
            "code": f"SOL-{suffix}",
            "name": "Solar",
            "kind": "solar",
            "capacity_mw": 250.0,
            "min_stable_mw": 0.0,
            "ramp_mw_per_min": 999.0,
            "marginal_cost": 0.0,
        },
        {
            "code": f"WND-{suffix}",
            "name": "Wind",
            "kind": "wind",
            "capacity_mw": 120.0,
            "min_stable_mw": 0.0,
            "ramp_mw_per_min": 999.0,
            "marginal_cost": 0.0,
        },
        {
            "code": f"BESS-{suffix}",
            "name": "Battery",
            "kind": "battery",
            "capacity_mw": 100.0,
            "min_stable_mw": 0.0,
            "ramp_mw_per_min": 999.0,
            "marginal_cost": 6100.0,
            "storage_capacity_mwh": 400.0,
            "max_charge_mw": 100.0,
            "max_discharge_mw": 100.0,
            "round_trip_efficiency": 0.88,
            "state_of_charge_mwh": 200.0,
        },
    ]
    for asset_payload in assets:
        response = client.post(
            f"/api/feeders/{feeder_id}/assets",
            json=asset_payload,
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text

    telemetry = client.post(
        f"/api/feeders/{feeder_id}/telemetry",
        json={
            "load_mw": 620.0,
            "frequency_hz": 50.0,
            "renewable_mw": 240.0,
            "renewable_potential_mw": 240.0,
        },
        headers=admin_headers,
    )
    assert telemetry.status_code == 201, telemetry.text

    return feeder_id
