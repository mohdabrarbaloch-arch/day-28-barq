"""Seed a demo grid so the console has something real to show.

Run with:  python -m scripts.seed

Idempotent: re-running replaces the demo feeder rather than stacking duplicates.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.database import SessionLocal, init_db
from app.core.security import hash_password
from app.core.types import utcnow
from app.models.grid import EnergyAsset, Feeder, TelemetryReading
from app.models.user import User

DEMO_EMAIL = "aisha@barq-example.com"
DEMO_PASSWORD = "barq-demo-2026"

# A plausible Karachi feeder: coal baseload, gas peaker, hydro, solar, wind and
# one battery. Costs are PKR/MWh and roughly track the real merit order.
ASSETS = [
    dict(
        code="COAL-K1",
        name="Port Qasim Coal",
        kind="thermal",
        capacity_mw=400.0,
        min_stable_mw=160.0,
        ramp_mw_per_min=6.0,
        marginal_cost=9800.0,
    ),
    dict(
        code="GAS-K1",
        name="Korangi Gas Peaker",
        kind="thermal",
        capacity_mw=220.0,
        min_stable_mw=80.0,
        ramp_mw_per_min=12.0,
        marginal_cost=14500.0,
    ),
    dict(
        code="HYD-K1",
        name="Indus Hydro",
        kind="hydro",
        capacity_mw=180.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=45.0,
        marginal_cost=3200.0,
    ),
    dict(
        code="SOL-K1",
        name="Jhimpir Solar",
        kind="solar",
        capacity_mw=250.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=999.0,
        marginal_cost=0.0,
    ),
    dict(
        code="WND-K1",
        name="Gharo Wind",
        kind="wind",
        capacity_mw=120.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=999.0,
        marginal_cost=0.0,
    ),
    dict(
        code="BESS-K1",
        name="Malir Battery",
        kind="battery",
        capacity_mw=100.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=999.0,
        marginal_cost=6100.0,
        storage_capacity_mwh=400.0,
        max_charge_mw=100.0,
        max_discharge_mw=100.0,
        round_trip_efficiency=0.88,
        state_of_charge_mwh=200.0,
    ),
]


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None:
            user = User(
                email=DEMO_EMAIL,
                full_name="Aisha Rahman",
                password_hash=hash_password(DEMO_PASSWORD),
                role="admin",
            )
            db.add(user)
            db.commit()
            print(f"created demo user {DEMO_EMAIL}")
        else:
            print(f"demo user {DEMO_EMAIL} already exists")

        feeder = db.scalar(select(Feeder).where(Feeder.code == "KHI-SOUTH-1"))
        if feeder is not None:
            print("demo feeder already exists — nothing to do")
            return

        feeder = Feeder(
            code="KHI-SOUTH-1",
            name="Karachi South",
            region="Karachi",
            peak_load_mw=900.0,
            industrial_share=0.45,
        )
        db.add(feeder)
        db.commit()
        db.refresh(feeder)

        for spec in ASSETS:
            db.add(EnergyAsset(feeder_id=feeder.id, **spec))
        db.commit()

        db.add(
            TelemetryReading(
                feeder_id=feeder.id,
                recorded_at=utcnow(),
                load_mw=620.0,
                frequency_hz=49.97,
                renewable_mw=240.0,
                renewable_potential_mw=310.0,
            )
        )
        db.commit()

        print(f"seeded feeder {feeder.code} with {len(ASSETS)} assets")
        print(f"sign in with {DEMO_EMAIL} / {DEMO_PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
