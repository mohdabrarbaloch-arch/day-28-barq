"""Grid endpoints: feeders, assets, telemetry, dispatch, alerts, dashboard."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_admin, require_operator
from app.core.types import utcnow
from app.models.grid import EnergyAsset, Feeder, TelemetryReading
from app.models.user import User
from app.schemas.grid import (
    AlertOut,
    AllocationOut,
    AssetAvailabilityUpdate,
    AssetCreate,
    AssetOut,
    DashboardOut,
    DispatchOut,
    DispatchRequest,
    FeederCreate,
    FeederOut,
    TelemetryCreate,
    TelemetryOut,
)
from app.services import grid_service
from app.services.grid_service import GridServiceError

router = APIRouter(prefix="/api", tags=["grid"])


def _service_error(exc: GridServiceError) -> HTTPException:
    """Map a domain error to a status code.

    'not found' is a 404; everything else is a 409, because the request was
    well-formed but conflicts with the state of the system.
    """
    message = str(exc)
    code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail=message)


# --------------------------------------------------------------------------
# Feeders
# --------------------------------------------------------------------------
@router.get("/feeders", response_model=list[FeederOut], summary="List feeders")
def list_feeders(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    active_only: bool = Query(default=True),
) -> list[FeederOut]:
    return [
        FeederOut.model_validate(f) for f in grid_service.list_feeders(db, active_only=active_only)
    ]


@router.post(
    "/feeders",
    response_model=FeederOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a feeder (operator)",
)
def create_feeder(
    payload: FeederCreate,
    _op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> FeederOut:
    feeder = Feeder(**payload.model_dump())
    db.add(feeder)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A feeder with code {payload.code} already exists",
        ) from exc
    db.refresh(feeder)
    return FeederOut.model_validate(feeder)


@router.get("/feeders/{feeder_id}", response_model=FeederOut, summary="Get a feeder")
def get_feeder(
    feeder_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> FeederOut:
    try:
        return FeederOut.model_validate(grid_service.get_feeder(db, feeder_id))
    except GridServiceError as exc:
        raise _service_error(exc) from exc


@router.delete(
    "/feeders/{feeder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Retire a feeder (admin)",
)
def delete_feeder(
    feeder_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Retire a feeder and everything attached to it.

    Decommissioning is a hard delete here because the cascade takes the assets,
    telemetry and plans with it. A production grid would soft-delete and keep
    the history; that is noted in the README rather than pretended away.
    """
    try:
        grid_service.delete_feeder(db, feeder_id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------
@router.get(
    "/feeders/{feeder_id}/assets",
    response_model=list[AssetOut],
    summary="List a feeder's assets",
)
def list_assets(
    feeder_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AssetOut]:
    try:
        grid_service.get_feeder(db, feeder_id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc
    return [AssetOut.model_validate(a) for a in grid_service.list_assets(db, feeder_id)]


@router.post(
    "/feeders/{feeder_id}/assets",
    response_model=AssetOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add an asset to a feeder (operator)",
)
def create_asset(
    feeder_id: int,
    payload: AssetCreate,
    _op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> AssetOut:
    try:
        grid_service.get_feeder(db, feeder_id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc

    asset = EnergyAsset(
        feeder_id=feeder_id,
        is_renewable=payload.kind in ("solar", "wind"),
        **payload.model_dump(),
    )
    db.add(asset)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An asset with code {payload.code} already exists",
        ) from exc
    db.refresh(asset)
    return AssetOut.model_validate(asset)


@router.patch(
    "/assets/{asset_id}/availability",
    response_model=AssetOut,
    summary="Take an asset in or out of service (operator)",
)
def set_availability(
    asset_id: int,
    payload: AssetAvailabilityUpdate,
    _op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> AssetOut:
    asset = db.get(EnergyAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    asset.is_available = payload.is_available
    db.commit()
    db.refresh(asset)
    return AssetOut.model_validate(asset)


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------
@router.post(
    "/feeders/{feeder_id}/telemetry",
    response_model=TelemetryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post a telemetry reading (operator)",
)
def post_telemetry(
    feeder_id: int,
    payload: TelemetryCreate,
    _op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> TelemetryOut:
    try:
        grid_service.get_feeder(db, feeder_id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc

    reading = TelemetryReading(
        feeder_id=feeder_id,
        recorded_at=payload.recorded_at or utcnow(),
        load_mw=payload.load_mw,
        frequency_hz=payload.frequency_hz,
        renewable_mw=payload.renewable_mw,
        renewable_potential_mw=payload.renewable_potential_mw,
        import_mw=payload.import_mw,
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return TelemetryOut.model_validate(reading)


@router.get(
    "/feeders/{feeder_id}/telemetry",
    response_model=list[TelemetryOut],
    summary="Recent telemetry for a feeder",
)
def get_telemetry(
    feeder_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[TelemetryOut]:
    try:
        grid_service.get_feeder(db, feeder_id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc
    rows = grid_service.reading_history(db, feeder_id, hours=hours, limit=limit)
    return [TelemetryOut.model_validate(r) for r in rows]


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def _to_dispatch_out(result, plan, feeder_id: int, interval_start: dt.datetime) -> DispatchOut:
    allocations = [
        AllocationOut(
            asset_id=0,
            asset_code=u.code,
            asset_kind=u.kind,
            setpoint_mw=round(u.setpoint_mw, 3),
            previous_mw=round(u.previous_mw, 3),
            delta_mw=round(u.delta_mw, 3),
            was_ramp_limited=u.ramp_limited,
            marginal_cost=u.marginal_cost,
            reason=u.reason,
        )
        for u in result.units
    ]
    return DispatchOut(
        plan_id=plan.id if plan else None,
        feeder_id=feeder_id,
        interval_start=interval_start,
        interval_minutes=result.interval_minutes,
        demand_mw=round(result.demand_mw, 3),
        generation_mw=round(result.generation_mw, 3),
        storage_net_mw=round(result.storage_net_mw, 3),
        curtailed_mw=round(result.curtailed_mw, 3),
        unserved_mw=round(result.unserved_mw, 3),
        expected_cost=round(result.expected_cost, 2),
        reserve_mw=round(result.reserve_mw, 3),
        reserve_required_mw=round(result.reserve_required_mw, 3),
        reserve_short_mw=round(result.reserve_short_mw, 3),
        status=result.status,
        frequency_hz=round(result.frequency_hz, 4),
        renewable_share=round(result.renewable_share, 4),
        marginal_price=result.marginal_price,
        allocations=allocations,
        notes=result.notes,
    )


@router.post(
    "/dispatch",
    response_model=DispatchOut,
    summary="Solve a dispatch interval (operator)",
)
def dispatch(
    payload: DispatchRequest,
    op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> DispatchOut:
    """Run the solver for one feeder and interval.

    Alerts are raised from the result, so a plan that leaves load unserved
    produces a critical alert in the same transaction rather than waiting for a
    separate monitoring pass.
    """
    try:
        result, plan = grid_service.run_dispatch(
            db,
            payload.feeder_id,
            demand_mw=payload.demand_mw,
            interval_minutes=payload.interval_minutes,
            persist=payload.persist,
            user_id=op.id,
        )
    except GridServiceError as exc:
        raise _service_error(exc) from exc

    if payload.persist:
        grid_service.evaluate_plan_alerts(db, payload.feeder_id, result)
        db.commit()

    return _to_dispatch_out(result, plan, payload.feeder_id, utcnow())


@router.post(
    "/dispatch/preview",
    response_model=DispatchOut,
    summary="Solve without saving (operator)",
)
def dispatch_preview(
    payload: DispatchRequest,
    _op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> DispatchOut:
    """What-if: solve the interval but write nothing.

    Useful for asking 'what would 900 MW cost me' without polluting the plan
    history or moving the battery's state of charge.
    """
    try:
        result, _ = grid_service.run_dispatch(
            db,
            payload.feeder_id,
            demand_mw=payload.demand_mw,
            interval_minutes=payload.interval_minutes,
            persist=False,
        )
    except GridServiceError as exc:
        raise _service_error(exc) from exc
    return _to_dispatch_out(result, None, payload.feeder_id, utcnow())


@router.get(
    "/feeders/{feeder_id}/plans",
    response_model=list[DispatchOut],
    summary="Recent dispatch plans",
)
def list_plans(
    feeder_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=200),
) -> list[DispatchOut]:
    try:
        grid_service.get_feeder(db, feeder_id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc

    plans = grid_service.recent_plans(db, feeder_id, limit=limit)
    out: list[DispatchOut] = []
    for plan in plans:
        allocations = [
            AllocationOut(
                asset_id=a.asset_id,
                asset_code=a.asset.code if a.asset else "?",
                asset_kind=a.asset.kind if a.asset else "?",
                setpoint_mw=round(a.setpoint_mw, 3),
                previous_mw=round(a.previous_mw, 3),
                delta_mw=round(a.delivered_delta_mw, 3),
                was_ramp_limited=a.was_ramp_limited,
                marginal_cost=a.marginal_cost,
                reason=a.reason,
            )
            for a in plan.allocations
        ]
        out.append(
            DispatchOut(
                plan_id=plan.id,
                feeder_id=plan.feeder_id,
                interval_start=plan.interval_start,
                interval_minutes=plan.interval_minutes,
                demand_mw=plan.demand_mw,
                generation_mw=plan.generation_mw,
                storage_net_mw=plan.storage_net_mw,
                curtailed_mw=plan.curtailed_mw,
                unserved_mw=plan.unserved_mw,
                expected_cost=plan.expected_cost,
                reserve_mw=plan.reserve_mw,
                reserve_required_mw=plan.reserve_required_mw,
                reserve_short_mw=plan.reserve_short_mw,
                status=plan.status,
                frequency_hz=plan.frequency_hz,
                renewable_share=0.0,
                marginal_price=0.0,
                allocations=allocations,
                notes=[],
            )
        )
    return out


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
@router.get("/alerts", response_model=list[AlertOut], summary="Open alerts")
def list_alerts(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    feeder_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AlertOut]:
    rows = grid_service.open_alerts(db, feeder_id=feeder_id, limit=limit)
    return [AlertOut.model_validate(a) for a in rows]


@router.post(
    "/alerts/{alert_id}/acknowledge",
    response_model=AlertOut,
    summary="Acknowledge an alert (operator)",
)
def acknowledge(
    alert_id: int,
    op: Annotated[User, Depends(require_operator)],
    db: Annotated[Session, Depends(get_db)],
) -> AlertOut:
    try:
        alert = grid_service.acknowledge_alert(db, alert_id, op.id)
    except GridServiceError as exc:
        raise _service_error(exc) from exc
    return AlertOut.model_validate(alert)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/dashboard", response_model=DashboardOut, summary="Fleet overview")
def dashboard(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DashboardOut:
    return DashboardOut.model_validate(grid_service.build_dashboard(db))
