"""Database-facing grid operations.

Keeps SQLAlchemy out of the routers and the solver out of the models. Everything
here takes a Session and returns ORM objects or plain values.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.types import utcnow
from app.models.grid import (
    DispatchAllocation,
    DispatchPlan,
    EnergyAsset,
    Feeder,
    GridAlert,
    TelemetryReading,
)
from app.services.dispatch import DispatchResult, UnitSpec, solve_dispatch


class GridServiceError(ValueError):
    """A domain rule was violated. Routers translate this to a 4xx."""


# --------------------------------------------------------------------------
# Feeders and assets
# --------------------------------------------------------------------------
def get_feeder(db: Session, feeder_id: int) -> Feeder:
    feeder = db.get(Feeder, feeder_id)
    if feeder is None:
        raise GridServiceError(f"Feeder {feeder_id} not found")
    return feeder


def list_feeders(db: Session, *, active_only: bool = True) -> Sequence[Feeder]:
    stmt = select(Feeder).order_by(Feeder.code)
    if active_only:
        stmt = stmt.where(Feeder.is_active.is_(True))
    return db.scalars(stmt).all()


def delete_feeder(db: Session, feeder_id: int) -> None:
    """Remove a feeder and everything hanging off it.

    The relationships are declared with cascade="all, delete-orphan", so the
    assets, telemetry and plans go with it. SQLite does not enforce foreign keys
    unless the pragma is on, which is why the cascade is expressed in the ORM
    rather than left to the database.
    """
    feeder = get_feeder(db, feeder_id)
    db.delete(feeder)
    db.commit()


def list_assets(db: Session, feeder_id: int) -> Sequence[EnergyAsset]:
    return db.scalars(
        select(EnergyAsset).where(EnergyAsset.feeder_id == feeder_id).order_by(EnergyAsset.code)
    ).all()


def latest_reading(db: Session, feeder_id: int) -> TelemetryReading | None:
    return db.scalars(
        select(TelemetryReading)
        .where(TelemetryReading.feeder_id == feeder_id)
        .order_by(TelemetryReading.recorded_at.desc())
        .limit(1)
    ).first()


def reading_history(
    db: Session, feeder_id: int, *, hours: int = 24, limit: int = 500
) -> Sequence[TelemetryReading]:
    since = utcnow() - dt.timedelta(hours=hours)
    return db.scalars(
        select(TelemetryReading)
        .where(
            TelemetryReading.feeder_id == feeder_id,
            TelemetryReading.recorded_at >= since,
        )
        .order_by(TelemetryReading.recorded_at.asc())
        .limit(limit)
    ).all()


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def _previous_setpoint(db: Session, asset: EnergyAsset) -> float:
    """The last setpoint this asset was given, or 0 if it has never run.

    Ramp limits are meaningless without a starting point, and defaulting to zero
    would let every unit jump to full output on the first solve. Reading the last
    allocation is what makes the second interval behave like a real one.
    """
    last = db.scalars(
        select(DispatchAllocation)
        .where(DispatchAllocation.asset_id == asset.id)
        .order_by(DispatchAllocation.id.desc())
        .limit(1)
    ).first()
    return last.setpoint_mw if last else 0.0


def build_unit_specs(
    db: Session, assets: Iterable[EnergyAsset], reading: TelemetryReading | None
) -> list[UnitSpec]:
    """Turn ORM assets into solver inputs.

    Renewable potential is apportioned from the feeder's latest reading by
    capacity share. That is an approximation and worth being explicit about: a
    real system reads each farm's own SCADA output. With one reading per feeder,
    capacity-weighted apportionment is the honest best available, and it keeps
    the total consistent with the measured number.
    """
    assets = list(assets)
    renewable_assets = [a for a in assets if a.is_renewable]
    total_renewable_capacity = sum(a.capacity_mw for a in renewable_assets) or 0.0

    potential_by_asset: dict[int, float] = {}
    if reading is not None and total_renewable_capacity > 0:
        for asset in renewable_assets:
            share = asset.capacity_mw / total_renewable_capacity
            potential_by_asset[asset.id] = reading.renewable_potential_mw * share

    specs: list[UnitSpec] = []
    for asset in assets:
        specs.append(
            UnitSpec(
                code=asset.code,
                kind=asset.kind,
                capacity_mw=asset.capacity_mw,
                min_stable_mw=asset.min_stable_mw,
                ramp_mw_per_min=asset.ramp_mw_per_min,
                marginal_cost=asset.marginal_cost,
                previous_mw=_previous_setpoint(db, asset),
                is_available=asset.is_available,
                potential_mw=potential_by_asset.get(asset.id),
                storage_capacity_mwh=asset.storage_capacity_mwh,
                max_charge_mw=asset.max_charge_mw,
                max_discharge_mw=asset.max_discharge_mw,
                round_trip_efficiency=asset.round_trip_efficiency,
                state_of_charge_mwh=asset.state_of_charge_mwh,
            )
        )
    return specs


def run_dispatch(
    db: Session,
    feeder_id: int,
    *,
    demand_mw: float | None = None,
    interval_minutes: int | None = None,
    persist: bool = True,
    user_id: int | None = None,
) -> tuple[DispatchResult, DispatchPlan | None]:
    """Solve one interval for a feeder, optionally storing the plan."""
    feeder = get_feeder(db, feeder_id)
    assets = list_assets(db, feeder_id)
    if not assets:
        raise GridServiceError(f"Feeder {feeder.code} has no assets; nothing to dispatch")

    reading = latest_reading(db, feeder_id)
    if demand_mw is None:
        if reading is None:
            raise GridServiceError(
                "No telemetry for this feeder and no demand_mw supplied. Post a "
                "reading first, or pass demand_mw explicitly."
            )
        demand_mw = reading.load_mw

    interval = interval_minutes or settings.dispatch_interval_minutes
    specs = build_unit_specs(db, assets, reading)

    result = solve_dispatch(
        specs,
        demand_mw,
        interval_minutes=interval,
        reserve_ratio=settings.reserve_margin_ratio,
        nominal_frequency_hz=settings.nominal_frequency_hz,
        grid_stiffness_mw_per_hz=settings.grid_stiffness_mw_per_hz,
    )

    if not persist:
        return result, None

    plan = DispatchPlan(
        feeder_id=feeder.id,
        created_by=user_id,
        interval_start=utcnow(),
        interval_minutes=interval,
        demand_mw=result.demand_mw,
        generation_mw=result.generation_mw,
        storage_net_mw=result.storage_net_mw,
        curtailed_mw=result.curtailed_mw,
        unserved_mw=result.unserved_mw,
        expected_cost=result.expected_cost,
        reserve_mw=result.reserve_mw,
        reserve_required_mw=result.reserve_required_mw,
        status=result.status,
        frequency_hz=result.frequency_hz,
    )
    db.add(plan)
    db.flush()

    by_code = {a.code: a for a in assets}
    for unit_plan in result.units:
        asset = by_code.get(unit_plan.code)
        if asset is None:  # pragma: no cover - solver only sees known assets
            continue
        db.add(
            DispatchAllocation(
                plan_id=plan.id,
                asset_id=asset.id,
                setpoint_mw=unit_plan.setpoint_mw,
                previous_mw=unit_plan.previous_mw,
                delivered_delta_mw=unit_plan.delta_mw,
                was_ramp_limited=unit_plan.ramp_limited,
                marginal_cost=unit_plan.marginal_cost,
                reason=unit_plan.reason,
            )
        )

    # Storage state of charge moves with the plan. Without this the battery
    # would be full forever and every later interval would look identical.
    for unit_plan in result.units:
        asset = by_code.get(unit_plan.code)
        if asset is None or not asset.is_storage:
            continue
        if asset.state_of_charge_mwh is None:
            continue
        new_soc = asset.state_of_charge_mwh + unit_plan.energy_delta_mwh
        asset.state_of_charge_mwh = max(0.0, min(asset.storage_capacity_mwh or 0.0, new_soc))

    db.commit()
    db.refresh(plan)
    return result, plan


def recent_plans(db: Session, feeder_id: int, *, limit: int = 20) -> Sequence[DispatchPlan]:
    return db.scalars(
        select(DispatchPlan)
        .where(DispatchPlan.feeder_id == feeder_id)
        .order_by(DispatchPlan.created_at.desc())
        .limit(limit)
    ).all()


def get_plan(db: Session, plan_id: int) -> DispatchPlan:
    plan = db.get(DispatchPlan, plan_id)
    if plan is None:
        raise GridServiceError(f"Plan {plan_id} not found")
    return plan


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
def raise_alert(
    db: Session,
    *,
    feeder_id: int | None,
    severity: str,
    kind: str,
    message: str,
    evidence: str | None = None,
    asset_id: int | None = None,
) -> GridAlert:
    alert = GridAlert(
        feeder_id=feeder_id,
        asset_id=asset_id,
        severity=severity,
        kind=kind,
        message=message,
        evidence=evidence,
    )
    db.add(alert)
    return alert


def evaluate_plan_alerts(db: Session, feeder_id: int, result: DispatchResult) -> list[GridAlert]:
    """Turn a dispatch result into alerts, without duplicating open ones.

    Duplicate suppression matters: a feeder that is short every interval would
    otherwise generate 96 identical critical alerts a day, and the operator would
    stop reading them - which is worse than no alert at all.
    """
    created: list[GridAlert] = []

    def _already_open(kind: str) -> bool:
        existing = db.scalars(
            select(GridAlert).where(
                GridAlert.feeder_id == feeder_id,
                GridAlert.kind == kind,
                GridAlert.acknowledged_at.is_(None),
            )
        ).first()
        return existing is not None

    if result.unserved_mw > 0.5 and not _already_open("unserved_energy"):
        created.append(
            raise_alert(
                db,
                feeder_id=feeder_id,
                severity="critical",
                kind="unserved_energy",
                message=(
                    f"{result.unserved_mw:.1f} MW of demand could not be served "
                    f"in the {result.interval_minutes}-minute interval."
                ),
                evidence=(
                    f"demand {result.demand_mw:.1f} MW, delivered "
                    f"{result.generation_mw + result.storage_net_mw:.1f} MW, "
                    f"status {result.status}"
                ),
            )
        )

    if result.reserve_short_mw > 0.5 and not _already_open("reserve_shortfall"):
        created.append(
            raise_alert(
                db,
                feeder_id=feeder_id,
                severity="warning",
                kind="reserve_shortfall",
                message=(
                    f"Reserve is {result.reserve_mw:.1f} MW against a "
                    f"{result.reserve_required_mw:.1f} MW N-1 requirement."
                ),
                evidence=(
                    f"short by {result.reserve_short_mw:.1f} MW; the requirement "
                    "is the larger of 8% of load and the biggest single unit"
                ),
            )
        )

    deviation = result.frequency_hz - settings.nominal_frequency_hz
    imbalance = result.generation_mw + result.storage_net_mw - result.demand_mw
    if abs(deviation) > 0.2:
        kind = "frequency_low" if deviation < 0 else "frequency_high"
        if not _already_open(kind):
            created.append(
                raise_alert(
                    db,
                    feeder_id=feeder_id,
                    severity="critical" if abs(deviation) > 0.5 else "warning",
                    kind=kind,
                    message=(
                        f"Frequency at {result.frequency_hz:.3f} Hz, "
                        f"{deviation:+.3f} Hz from nominal."
                    ),
                    evidence=(
                        "residual imbalance "
                        f"{imbalance:+.1f} MW "
                        f"at {settings.grid_stiffness_mw_per_hz:.0f} MW/Hz stiffness"
                    ),
                )
            )

    if result.curtailed_mw > 0.5 and not _already_open("curtailment"):
        created.append(
            raise_alert(
                db,
                feeder_id=feeder_id,
                severity="info",
                kind="curtailment",
                message=(f"{result.curtailed_mw:.1f} MW of renewable output curtailed."),
                evidence=(
                    "storage was full or absent, and renewables alone exceeded "
                    f"demand of {result.demand_mw:.1f} MW"
                ),
            )
        )

    return created


def open_alerts(
    db: Session, *, feeder_id: int | None = None, limit: int = 100
) -> Sequence[GridAlert]:
    stmt = select(GridAlert).where(GridAlert.acknowledged_at.is_(None))
    if feeder_id is not None:
        stmt = stmt.where(GridAlert.feeder_id == feeder_id)
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    rows = db.scalars(stmt.order_by(GridAlert.created_at.desc()).limit(limit)).all()
    return sorted(rows, key=lambda a: (severity_rank.get(a.severity, 3), -a.id))


def acknowledge_alert(db: Session, alert_id: int, user_id: int) -> GridAlert:
    alert = db.get(GridAlert, alert_id)
    if alert is None:
        raise GridServiceError(f"Alert {alert_id} not found")
    if alert.acknowledged_at is not None:
        raise GridServiceError(f"Alert {alert_id} is already acknowledged")
    alert.acknowledged_at = utcnow()
    alert.acknowledged_by = user_id
    db.commit()
    db.refresh(alert)
    return alert


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def build_dashboard(db: Session) -> dict:
    """One query per feeder is fine at this scale and keeps the code readable.

    At a few thousand feeders this becomes a single aggregate query; the shape of
    the response would not change.
    """
    feeders = list_feeders(db, active_only=True)
    summaries = []
    total_load = total_capacity = total_renewable = total_curtailed = 0.0
    frequencies: list[float] = []

    for feeder in feeders:
        reading = latest_reading(db, feeder.id)
        assets = list_assets(db, feeder.id)
        capacity = sum(a.capacity_mw for a in assets if a.is_available)
        load = reading.load_mw if reading else 0.0
        renewable = reading.renewable_mw if reading else 0.0
        curtailed = reading.curtailed_mw if reading else 0.0
        frequency = reading.frequency_hz if reading else settings.nominal_frequency_hz

        alert_count = db.scalar(
            select(func.count(GridAlert.id)).where(
                GridAlert.feeder_id == feeder.id,
                GridAlert.acknowledged_at.is_(None),
            )
        )

        total_load += load
        total_capacity += capacity
        total_renewable += renewable
        total_curtailed += curtailed
        frequencies.append(frequency)

        summaries.append(
            {
                "feeder_id": feeder.id,
                "code": feeder.code,
                "name": feeder.name,
                "region": feeder.region,
                "load_mw": round(load, 2),
                "frequency_hz": round(frequency, 4),
                "renewable_mw": round(renewable, 2),
                "curtailed_mw": round(curtailed, 2),
                "capacity_mw": round(capacity, 2),
                "headroom_mw": round(max(0.0, capacity - load), 2),
                "utilisation": round(load / capacity, 4) if capacity > 0 else 0.0,
                "open_alerts": int(alert_count or 0),
                "recorded_at": reading.recorded_at if reading else None,
            }
        )

    open_count = db.scalar(
        select(func.count(GridAlert.id)).where(GridAlert.acknowledged_at.is_(None))
    )
    critical_count = db.scalar(
        select(func.count(GridAlert.id)).where(
            GridAlert.acknowledged_at.is_(None),
            GridAlert.severity == "critical",
        )
    )

    return {
        "generated_at": utcnow(),
        "total_load_mw": round(total_load, 2),
        "total_capacity_mw": round(total_capacity, 2),
        "total_renewable_mw": round(total_renewable, 2),
        "total_curtailed_mw": round(total_curtailed, 2),
        "average_frequency_hz": (
            round(sum(frequencies) / len(frequencies), 4) if frequencies else 0.0
        ),
        "renewable_share": (round(total_renewable / total_load, 4) if total_load > 0 else 0.0),
        "open_alerts": int(open_count or 0),
        "critical_alerts": int(critical_count or 0),
        "feeders": summaries,
    }
