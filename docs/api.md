# API reference

Base path: `/api`. All request and response bodies are JSON.

Authentication is a bearer token in the `Authorization` header:

```
Authorization: Bearer <access_token>
```

Interactive docs are served at `/docs` (Swagger) and `/redoc`.

---

## Errors

Every error returns a consistent shape:

```json
{ "detail": "Feeder KHI-SOUTH-1 already exists" }
```

Validation errors (422) use FastAPI's structured form:

```json
{
  "detail": {
    "errors": [
      { "field": "demand_mw", "message": "Input should be greater than or equal to 0" }
    ]
  }
}
```

| Code | Meaning |
|---|---|
| 400 | Malformed request |
| 401 | Missing, expired or invalid token |
| 403 | Authenticated but the role is insufficient |
| 404 | Resource does not exist |
| 409 | Conflict — duplicate code, or an operation that would lock you out |
| 422 | Validation failure |
| 429 | Rate limited |

---

## Health

### `GET /health`

Public. No token required.

```json
{ "status": "ok", "version": "1.0.0", "environment": "development" }
```

---

## Authentication

### `POST /api/auth/register`

Creates an account. **The first account on a fresh database becomes the admin**;
every later account is a viewer.

```json
{
  "email": "operator@barq.pk",
  "password": "at-least-eight-characters",
  "full_name": "Operator Name"
}
```

`201 Created`

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "user": { "id": 2, "email": "operator@barq.pk", "full_name": "Operator Name", "role": "viewer" }
}
```

`409` if the email is taken. `422` if the password is under eight characters or
the email is malformed.

### `POST /api/auth/login`

```json
{ "email": "operator@barq.pk", "password": "at-least-eight-characters" }
```

`200` with the same body as register. `401` on a wrong password **or an unknown
email** — the two are deliberately indistinguishable so the endpoint cannot be
used to enumerate accounts.

### `GET /api/auth/me`

`200` with the current user. `401` without a valid token.

### `GET /api/auth/users` — admin

Lists accounts. `403` for anyone else.

### `PATCH /api/auth/users/{id}/role` — admin

```json
{ "role": "operator" }
```

`200` on success. `409` if an admin tries to demote themselves — that is the
only way to lock yourself out of the last admin account, so it is refused.

---

## Feeders

### `GET /api/feeders`

`200` with a list. Any authenticated role.

```json
[
  {
    "id": 1,
    "code": "KHI-SOUTH-1",
    "name": "Karachi South",
    "region": "Karachi",
    "peak_load_mw": 900.0,
    "industrial_share": 0.45
  }
]
```

### `POST /api/feeders` — admin

```json
{
  "code": "KHI-NORTH-1",
  "name": "Karachi North",
  "region": "Karachi",
  "peak_load_mw": 750.0,
  "industrial_share": 0.3
}
```

`201` on success, `409` on a duplicate code, `403` for non-admins.

### `DELETE /api/feeders/{id}` — admin

`204` on success. Cascades to assets, telemetry, plans and alerts. `404` if it
does not exist.

### `GET /api/feeders/{id}/assets`

`200` with the units on that feeder.

---

## Assets

### `POST /api/assets` — admin

```json
{
  "feeder_id": 1,
  "code": "COAL-K2",
  "name": "Port Qasim Unit 2",
  "kind": "thermal",
  "capacity_mw": 400.0,
  "min_stable_mw": 160.0,
  "ramp_mw_per_min": 6.0,
  "marginal_cost": 9800.0
}
```

`kind` is one of `thermal`, `hydro`, `solar`, `wind`, `battery`.

Storage units also take `storage_capacity_mwh`, `max_charge_mw`,
`max_discharge_mw`, `round_trip_efficiency` and `state_of_charge_mwh`.

`201` on success. `404` if the feeder does not exist. `422` if
`min_stable_mw > capacity_mw` — a unit that cannot run at its own minimum is a
data error, not a configuration.

---

## Telemetry

### `POST /api/telemetry` — operator or admin

```json
{
  "asset_id": 1,
  "output_mw": 88.5,
  "frequency_hz": 50.01
}
```

`201` on success. `404` for a missing asset. `422` if frequency is outside
40–60 Hz — that is a sensor fault, not a grid event.

### `GET /api/telemetry/{asset_id}?limit=100`

`200` with readings, newest first. `limit` is capped at 1000.

---

## Dispatch

### `POST /api/dispatch` — operator or admin

Solves an interval and persists the plan.

```json
{
  "feeder_id": 1,
  "demand_mw": 620.0,
  "interval_minutes": 30,
  "persist": true
}
```

`201 Created`

```json
{
  "id": 7,
  "feeder_id": 1,
  "demand_mw": 620.0,
  "interval_minutes": 30,
  "status": "surplus",
  "generation_mw": 520.0,
  "storage_net_mw": 100.0,
  "curtailed_mw": 110.0,
  "unserved_mw": 0.0,
  "reserve_mw": 30.0,
  "reserve_required_mw": 400.0,
  "reserve_short_mw": 370.0,
  "expected_cost": 672000.0,
  "marginal_price": 3200.0,
  "frequency_hz": 49.97,
  "notes": [
    "Coal COAL-K1 is held at 210.0 MW by its 6.0 MW/min ramp limit; the surplus cannot be absorbed."
  ],
  "allocations": [
    {
      "asset_code": "HYD-K1",
      "kind": "hydro",
      "setpoint_mw": 210.0,
      "capacity_mw": 180.0,
      "marginal_cost": 3200.0,
      "was_ramp_limited": false,
      "reason": "Merit position: PKR 3,200/MWh. Set to 210.0 MW."
    }
  ]
}
```

`status` is `balanced`, `surplus` or `deficit`.

`422` if the feeder has no assets — there is nothing to dispatch.

### `POST /api/dispatch/preview`

Identical body and response, but **writes nothing**. Use this while exploring.

### `GET /api/dispatch/{id}`

`200` with a stored plan. `404` if it does not exist.

### `GET /api/dispatch?feeder_id=1&limit=20`

`200` with recent plans for a feeder, newest first.

---

## Alerts

### `GET /api/alerts?limit=50`

`200` with open alerts, newest first.

```json
[
  {
    "id": 3,
    "feeder_id": 1,
    "severity": "critical",
    "title": "Reserve shortfall",
    "message": "30 MW of spinning reserve against a 400 MW requirement (370 MW short).",
    "created_at": "2026-11-20T00:14:00Z",
    "acknowledged_at": null
  }
]
```

`severity` is `info`, `warning` or `critical`.

### `POST /api/alerts/{id}/acknowledge` — admin

`200` with the updated alert. Sets `acknowledged_at`; does not delete the row.

---

## Dashboard

### `GET /api/dashboard`

`200` with a summary for the console.

```json
{
  "feeder_count": 1,
  "asset_count": 6,
  "total_capacity_mw": 1270.0,
  "total_load_mw": 620.0,
  "renewable_share": 0.387,
  "average_frequency_hz": 49.97,
  "open_alerts": 1,
  "critical_alerts": 1
}
```

---

## Rate limits

| Scope | Limit |
|---|---|
| `/api/auth/*` | 10 requests per minute per IP |
| Everything else | 120 requests per minute per IP |

Exceeding either returns `429` with a `Retry-After` header. The limiter is
in-process, so N workers means N× the limit — see the scaling notes in
`ARCHITECTURE.md`.
