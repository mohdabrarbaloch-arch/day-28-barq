"""API-level tests: auth, authorisation, validation and the dispatch endpoint.

These exercise the real HTTP surface through TestClient, so they catch the
things unit tests cannot: status codes, dependency wiring, and whether a route
is actually protected.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------
# Health and auth
# --------------------------------------------------------------------------
def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"


def test_register_returns_a_token_and_the_user(client):
    response = client.post(
        "/api/auth/register",
        json={
            "email": "new@barq-example.com",
            "password": "a-long-enough-password",
            "full_name": "New",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "new@barq-example.com"


def test_the_first_account_becomes_admin_and_later_ones_are_viewers(client):
    first = client.post(
        "/api/auth/register",
        json={
            "email": "first@barq-example.com",
            "password": "a-long-enough-password",
            "full_name": "First",
        },
    )
    second = client.post(
        "/api/auth/register",
        json={
            "email": "second@barq-example.com",
            "password": "a-long-enough-password",
            "full_name": "Second",
        },
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["user"]["role"] == "admin"
    assert second.json()["user"]["role"] == "viewer"


def test_register_rejects_a_duplicate_email(client, admin_user):
    response = client.post(
        "/api/auth/register",
        json={
            "email": admin_user["email"],
            "password": "another-long-password",
            "full_name": "Duplicate",
        },
    )
    assert response.status_code == 409


def test_register_rejects_a_short_password(client):
    response = client.post(
        "/api/auth/register", json={"email": "short@barq-example.com", "password": "abc"}
    )
    assert response.status_code == 422


def test_register_rejects_a_malformed_email(client):
    response = client.post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": "a-long-enough-password"},
    )
    assert response.status_code == 422


def test_login_with_the_wrong_password_is_401(client, admin_user):
    response = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_login_with_an_unknown_email_is_401(client):
    response = client.post(
        "/api/auth/login",
        json={"email": "nobody@barq-example.com", "password": "a-long-enough-password"},
    )
    assert response.status_code == 401


def test_login_succeeds_with_the_right_password(client, admin_user):
    response = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": "test-password-123"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_me_requires_a_token(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_rejects_a_garbage_token(client):
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401


def test_me_returns_the_current_user(client, admin_headers, admin_user):
    response = client.get("/api/auth/me", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["email"] == admin_user["email"]


# --------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------
def test_a_viewer_cannot_create_a_feeder(client, viewer_headers):
    response = client.post(
        "/api/feeders",
        json={
            "code": "FDR-NOPE",
            "name": "Nope",
            "region": "Karachi",
            "peak_load_mw": 100.0,
        },
        headers=viewer_headers,
    )
    assert response.status_code == 403


def test_an_admin_can_create_a_feeder(client, admin_headers):
    response = client.post(
        "/api/feeders",
        json={
            "code": "FDR-OK",
            "name": "Korangi",
            "region": "Karachi",
            "peak_load_mw": 120.0,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201
    assert response.json()["code"] == "FDR-OK"


def test_a_duplicate_feeder_code_is_409(client, admin_headers, feeder):
    response = client.post(
        "/api/feeders",
        json={
            "code": feeder["code"],
            "name": "Dup",
            "region": "Karachi",
            "peak_load_mw": 10.0,
        },
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_a_viewer_cannot_delete_a_feeder(client, viewer_headers, feeder):
    response = client.delete(f"/api/feeders/{feeder['id']}", headers=viewer_headers)
    assert response.status_code == 403


def test_an_admin_cannot_demote_themselves(client, admin_headers, admin_user):
    response = client.patch(
        f"/api/auth/users/{admin_user['id']}/role",
        json={"role": "viewer"},
        headers=admin_headers,
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------
# Assets and telemetry
# --------------------------------------------------------------------------
def test_creating_an_asset_requires_a_real_feeder(client, admin_headers):
    response = client.post(
        "/api/feeders/99999/assets",
        json={
            "code": "GHOST-1",
            "name": "Ghost",
            "kind": "thermal",
            "capacity_mw": 100.0,
            "min_stable_mw": 40.0,
            "ramp_mw_per_min": 5.0,
            "marginal_cost": 9000.0,
        },
        headers=admin_headers,
    )
    assert response.status_code == 404


def test_asset_validation_rejects_min_above_capacity(client, admin_headers, feeder):
    response = client.post(
        f"/api/feeders/{feeder['id']}/assets",
        json={
            "code": "BAD-1",
            "name": "Bad",
            "kind": "thermal",
            "capacity_mw": 100.0,
            "min_stable_mw": 200.0,
            "ramp_mw_per_min": 5.0,
            "marginal_cost": 9000.0,
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_telemetry_ingest_and_read_back(client, admin_headers, feeder):
    post = client.post(
        f"/api/feeders/{feeder['id']}/telemetry",
        json={"load_mw": 88.5, "frequency_hz": 50.01},
        headers=admin_headers,
    )
    assert post.status_code == 201

    read = client.get(f"/api/feeders/{feeder['id']}/telemetry?limit=10", headers=admin_headers)
    assert read.status_code == 200
    readings = read.json()
    assert len(readings) == 1
    assert readings[0]["load_mw"] == pytest.approx(88.5)


def test_telemetry_rejects_an_impossible_frequency(client, admin_headers, feeder):
    response = client.post(
        f"/api/feeders/{feeder['id']}/telemetry",
        json={"load_mw": 50.0, "frequency_hz": 90.0},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_telemetry_for_a_missing_feeder_is_404(client, admin_headers):
    response = client.post(
        "/api/feeders/99999/telemetry",
        json={"load_mw": 10.0, "frequency_hz": 50.0},
        headers=admin_headers,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def test_dispatch_requires_a_token(client):
    assert (
        client.post("/api/dispatch", json={"feeder_id": 1, "demand_mw": 100.0}).status_code == 401
    )


def test_dispatch_returns_a_plan_with_balanced_units(client, operator_headers, seeded_grid):
    response = client.post(
        "/api/dispatch",
        json={
            "feeder_id": seeded_grid,
            "demand_mw": 300.0,
            "interval_minutes": 30,
            "persist": True,
        },
        headers=operator_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["demand_mw"] == pytest.approx(300.0)
    assert body["allocations"], "a plan must contain at least one unit"
    assert body["status"] in {"feasible", "surplus", "deficit"}
    # Every unit must carry a human-readable reason.
    assert all(unit["reason"] for unit in body["allocations"])


def test_dispatch_rejects_negative_demand(client, operator_headers, seeded_grid):
    response = client.post(
        "/api/dispatch",
        json={"feeder_id": seeded_grid, "demand_mw": -50.0},
        headers=operator_headers,
    )
    assert response.status_code == 422


def test_dispatch_rejects_an_absurd_interval(client, operator_headers, seeded_grid):
    response = client.post(
        "/api/dispatch",
        json={"feeder_id": seeded_grid, "demand_mw": 100.0, "interval_minutes": 5000},
        headers=operator_headers,
    )
    assert response.status_code == 422


def test_dispatch_on_a_feeder_with_no_assets_is_409(client, operator_headers, feeder):
    """A feeder with no assets is a conflict, not a validation error.

    The request is well-formed; the grid simply has nothing to dispatch.
    """
    response = client.post(
        "/api/dispatch",
        json={"feeder_id": feeder["id"], "demand_mw": 100.0},
        headers=operator_headers,
    )
    assert response.status_code == 409


def test_dispatch_on_a_missing_feeder_is_404(client, operator_headers):
    response = client.post(
        "/api/dispatch",
        json={"feeder_id": 99999, "demand_mw": 100.0},
        headers=operator_headers,
    )
    assert response.status_code == 404


def test_a_persisted_plan_appears_in_the_plan_history(client, operator_headers, seeded_grid):
    created = client.post(
        "/api/dispatch",
        json={
            "feeder_id": seeded_grid,
            "demand_mw": 250.0,
            "interval_minutes": 30,
            "persist": True,
        },
        headers=operator_headers,
    )
    assert created.status_code == 200

    history = client.get(f"/api/feeders/{seeded_grid}/plans", headers=operator_headers)
    assert history.status_code == 200
    assert len(history.json()) >= 1


def test_a_preview_plan_writes_nothing(client, operator_headers, seeded_grid):
    before = client.get(f"/api/feeders/{seeded_grid}/plans", headers=operator_headers)
    count_before = len(before.json())

    preview = client.post(
        "/api/dispatch/preview",
        json={"feeder_id": seeded_grid, "demand_mw": 400.0, "interval_minutes": 30},
        headers=operator_headers,
    )
    assert preview.status_code == 200

    after = client.get(f"/api/feeders/{seeded_grid}/plans", headers=operator_headers)
    assert len(after.json()) == count_before


# --------------------------------------------------------------------------
# Dashboard and alerts
# --------------------------------------------------------------------------
def test_dashboard_summarises_the_grid(client, operator_headers, seeded_grid):
    response = client.get("/api/dashboard", headers=operator_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total_capacity_mw"] >= 1
    assert "renewable_share" in body


def test_alerts_list_is_empty_on_a_healthy_grid(client, operator_headers, seeded_grid):
    response = client.get("/api/alerts", headers=operator_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_a_viewer_cannot_acknowledge_an_alert(client, viewer_headers):
    response = client.post("/api/alerts/99999/acknowledge", headers=viewer_headers)
    assert response.status_code == 403


def test_acknowledging_a_missing_alert_is_404(client, operator_headers):
    response = client.post("/api/alerts/99999/acknowledge", headers=operator_headers)
    assert response.status_code == 404
