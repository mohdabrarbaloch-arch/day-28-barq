# Architecture

Barq is a dispatch and monitoring console for a distribution feeder. It answers
one question well: **given this fleet and this demand, what should each unit be
set to, and why?**

The "why" is the point. Every setpoint carries a sentence naming the constraint
that produced it — merit position, ramp limit, minimum stable load, storage
energy, or curtailment. A dispatcher who disagrees with a number can see which
assumption to argue with.

---

## System diagram

```
                        ┌──────────────────────────┐
                        │   Browser (vanilla JS)   │
                        │  index.html + app.js     │
                        │  no build step           │
                        └────────────┬─────────────┘
                                     │  JSON over HTTPS
                                     │  Bearer JWT
                        ┌────────────▼─────────────┐
                        │        FastAPI           │
                        │  ┌────────────────────┐  │
                        │  │  api/auth.py       │  │  register, login, me,
                        │  │                    │  │  users, role changes
                        │  ├────────────────────┤  │
                        │  │  api/grid.py       │  │  feeders, assets,
                        │  │                    │  │  telemetry, dispatch,
                        │  │                    │  │  plans, alerts, dashboard
                        │  └─────────┬──────────┘  │
                        │            │             │
                        │  ┌─────────▼──────────┐  │
                        │  │ services/          │  │
                        │  │  dispatch.py       │  │  ← pure functions
                        │  │  grid_service.py   │  │  ← DB orchestration
                        │  └─────────┬──────────┘  │
                        │            │             │
                        │  ┌─────────▼──────────┐  │
                        │  │  SQLAlchemy 2.0    │  │
                        │  └─────────┬──────────┘  │
                        └────────────┼─────────────┘
                                     │
                        ┌────────────▼─────────────┐
                        │  PostgreSQL (prod)       │
                        │  SQLite (dev / tests)    │
                        └──────────────────────────┘
```

---

## The dispatch solver

`app/services/dispatch.py` is the heart of the project and deliberately has **no
database, no I/O and no framework imports**. It takes a list of `UnitSpec`
dataclasses and a demand figure, and returns a `DispatchResult`. That makes it
testable in microseconds and reusable from a notebook, a CLI or a scheduler.

### Order of operations

The order is the real operational order, not a convenient one:

1. **Must-run renewables** at their available output, capped at nameplate.
2. **Surplus handling.** If renewables alone exceed demand, charge storage
   first, then curtail whatever the batteries cannot absorb. Curtailing before
   charging would throw away free energy that a battery could have stored.
3. **Storage discharge** into any remaining deficit, limited by both power
   rating and stored energy (with round-trip efficiency applied).
4. **Merit order** over dispatchable units, cheapest first, respecting each
   unit's achievable band this interval.
5. **Min-stable overshoot.** If the gap is smaller than any unit's floor, commit
   the cheapest one at its minimum and report the excess as genuine
   over-generation.
6. **Reserve, cost and frequency** computed from the resulting plan.

### The achievable band

This is the part that is easy to get wrong, and the first version did.

Each unit has a band this interval, and the band is **not symmetric**:

```
running:   floor = max(min_stable, previous - ramp_budget)
           ceiling = min(capacity, previous + ramp_budget)

cold:      floor = 0
           ceiling = min(capacity, ramp_budget)
```

The floor is *forced* output. A 400 MW coal block sitting at 300 MW cannot drop
to zero in fifteen minutes — at 6 MW/min it can only reach 210. Modelling only
the upward direction, which is the obvious way to write this, lets a coal unit
teleport to zero the moment demand falls. That was a real bug in this codebase,
caught by a test that asserted the ramp limit applies downwards too.

The floor is also why a system with plenty of cheap capacity can still show
over-generation: the coal is stuck above demand and there is nowhere for the
energy to go.

### Reserve

Spinning reserve is headroom on units that are **actually running**, and only as
much of it as they can reach inside the reserve window:

```
reserve += min(capacity - setpoint, ramp_mw_per_min * reserve_window_minutes)
```

The first version counted `capacity - setpoint` for every available unit, which
credited a cold 400 MW block sitting at 0 MW with 400 MW of "reserve" it could
not deliver. On the demo grid that reported 650 MW of reserve against a 400 MW
requirement — a comfortable pass on a grid that was actually 370 MW short. The
corrected figure is 30 MW, and the alert fires.

---

## Data model

```
User ──┐
       │
Feeder ─┼──< EnergyAsset
       ├──< TelemetryReading
       ├──< DispatchPlan ──< DispatchAllocation
       └──< GridAlert
```

| Table | Purpose |
|---|---|
| `users` | Accounts, bcrypt hashes, role (`admin` / `operator` / `viewer`) |
| `feeders` | A distribution feeder: code, region, peak load, industrial share |
| `energy_assets` | Generating units and storage, with their physical limits |
| `telemetry_readings` | Time series: load, frequency, renewable output |
| `dispatch_plans` | One solved interval, with totals and status |
| `dispatch_allocations` | Per-unit setpoint and the reason for it |
| `grid_alerts` | Raised by the solver, with severity and lifecycle |

Cascades are declared in the ORM (`cascade="all, delete-orphan"`) rather than
left to the database, because SQLite does not enforce foreign keys unless the
pragma is on.

---

## Authentication and authorisation

- **JWT** bearer tokens, HS256, signed with `SECRET_KEY` from the environment.
- **bcrypt** password hashing, cost configurable (`BCRYPT_ROUNDS`, lowered in
  tests so the suite stays fast).
- **Three roles.** `viewer` reads. `operator` writes telemetry and runs
  dispatch. `admin` manages feeders, assets and user roles.
- **Bootstrap rule:** the first account registered on a fresh database becomes
  the admin. Every later account is a viewer until promoted.
- **Self-demotion is refused** (409). Locking yourself out of the only admin
  account is unrecoverable without database access.

---

## Security

| Concern | Handling |
|---|---|
| Secrets | Environment only. `SECRET_KEY` is validated at startup and the app refuses to boot in production with a weak or default value. |
| Password storage | bcrypt, never plaintext, never reversible. |
| Rate limiting | In-process sliding window, separate budget for `/api/auth/*`. |
| Security headers | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, HSTS in production. |
| CORS | Explicit allow-list from `CORS_ORIGINS`, not `*`. |
| Input validation | Pydantic models with bounds on every numeric field. |
| SQL injection | Parameterised queries throughout via SQLAlchemy. |
| Error leakage | Service errors map to HTTP codes; stack traces never reach the client. |

---

## Scaling notes

Honest about what this is and is not.

**What scales today.** The solver is O(n log n) in unit count and stateless, so
it can run per-request or in a worker pool. The API is stateless apart from the
database, so it scales horizontally behind a load balancer.

**What does not, and what to do about it.**

1. **Rate limiting is per-process.** N workers means N× the limit. Move the
   counter to Redis (`INCR` with a TTL) before running more than one worker.
2. **The solver is single-interval.** Real dispatch is a multi-period problem:
   committing a unit now constrains what is available in three hours. A
   production system solves a 24-hour horizon with a MILP (unit commitment) and
   uses this merit-order pass as the intra-interval economic dispatch. The
   interface is designed for that — `solve_dispatch` takes one interval and
   returns one plan, so a horizon solver can call it in a loop or replace it.
3. **No migrations.** `create_all()` is fine for one deploy and wrong for a
   schema that will change. Alembic is the answer.
4. **Telemetry is polled, not streamed.** A real SCADA feed pushes. The ingest
   endpoint is a plain POST, so a WebSocket or MQTT bridge can sit in front of
   it without changing the model.
5. **Deletes are hard.** Decommissioning a feeder removes its history. A
   production grid soft-deletes and keeps the audit trail.

---

## Testing strategy

77 tests, no network, no external services.

- **`tests/test_dispatch.py` (42 tests)** — the solver. Merit order, ramp limits
  in both directions, minimum stable load, curtailment, storage energy limits,
  round-trip efficiency, reserve adequacy, frequency response, cost arithmetic,
  and the explanation text itself.
- **`tests/test_api.py` (35 tests)** — the HTTP surface. Status codes, role
  gates, validation rejections, the first-user-becomes-admin rule, dispatch
  persistence, and that a preview writes nothing.

Each test gets a clean database via an autouse fixture. Without it, "the first
account becomes admin" is only true for whichever test runs first, and every
later test silently gets a viewer — the kind of order-dependent failure that
makes a suite untrustworthy.
