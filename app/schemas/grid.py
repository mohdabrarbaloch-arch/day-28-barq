"""Grid request/response models."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetKind = Literal["thermal", "hydro", "solar", "wind", "battery"]
Severity = Literal["info", "warning", "critical"]


# --------------------------------------------------------------------------
# Feeders
# --------------------------------------------------------------------------
class FeederCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    region: str = Field(min_length=2, max_length=80)
    peak_load_mw: float = Field(gt=0, le=100_000)
    industrial_share: float = Field(default=0.3, ge=0, le=1)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.upper()


class FeederOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    region: str
    peak_load_mw: float
    industrial_share: float
    is_active: bool
    created_at: dt.datetime


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------
class AssetCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    kind: AssetKind
    capacity_mw: float = Field(gt=0, le=100_000)
    min_stable_mw: float = Field(default=0.0, ge=0)
    ramp_mw_per_min: float = Field(gt=0, le=10_000)
    marginal_cost: float = Field(ge=0, le=1_000_000)

    storage_capacity_mwh: float | None = Field(default=None, gt=0)
    max_charge_mw: float | None = Field(default=None, gt=0)
    max_discharge_mw: float | None = Field(default=None, gt=0)
    round_trip_efficiency: float | None = Field(default=None, gt=0, le=1)
    state_of_charge_mwh: float | None = Field(default=None, ge=0)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.min_stable_mw > self.capacity_mw:
            raise ValueError("min_stable_mw cannot exceed capacity_mw")

        if self.kind == "battery":
            missing = [
                field
                for field in (
                    "storage_capacity_mwh",
                    "max_charge_mw",
                    "max_discharge_mw",
                    "round_trip_efficiency",
                )
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError("A battery requires " + ", ".join(missing) + " to be set")
            if (self.state_of_charge_mwh or 0) > (self.storage_capacity_mwh or 0):
                raise ValueError("state_of_charge_mwh cannot exceed storage_capacity_mwh")
        else:
            # Generators must not carry storage fields: a solar farm with a
            # state of charge is a data-entry mistake, and silently ignoring it
            # would hide the mistake in every later report.
            stray = [
                field
                for field in (
                    "storage_capacity_mwh",
                    "max_charge_mw",
                    "max_discharge_mw",
                    "round_trip_efficiency",
                    "state_of_charge_mwh",
                )
                if getattr(self, field) is not None
            ]
            if stray:
                raise ValueError(
                    f"{self.kind} assets cannot set storage fields: " + ", ".join(stray)
                )
        return self


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    feeder_id: int
    kind: AssetKind
    capacity_mw: float
    min_stable_mw: float
    ramp_mw_per_min: float
    marginal_cost: float
    storage_capacity_mwh: float | None = None
    max_charge_mw: float | None = None
    max_discharge_mw: float | None = None
    round_trip_efficiency: float | None = None
    state_of_charge_mwh: float | None = None
    is_renewable: bool
    is_available: bool
    created_at: dt.datetime


class AssetAvailabilityUpdate(BaseModel):
    is_available: bool


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------
class TelemetryCreate(BaseModel):
    recorded_at: dt.datetime | None = None
    load_mw: float = Field(ge=0, le=100_000)
    frequency_hz: float = Field(ge=40, le=60)
    renewable_mw: float = Field(default=0.0, ge=0, le=100_000)
    renewable_potential_mw: float = Field(default=0.0, ge=0, le=100_000)
    import_mw: float = Field(default=0.0, ge=-100_000, le=100_000)

    @model_validator(mode="after")
    def _renewable_within_potential(self):
        if self.renewable_mw > self.renewable_potential_mw + 1e-6:
            raise ValueError(
                "renewable_mw cannot exceed renewable_potential_mw: delivered "
                "output is potential minus curtailment"
            )
        return self


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    feeder_id: int
    recorded_at: dt.datetime
    load_mw: float
    frequency_hz: float
    renewable_mw: float
    renewable_potential_mw: float
    import_mw: float
    curtailed_mw: float


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
class DispatchRequest(BaseModel):
    feeder_id: int = Field(gt=0)
    demand_mw: float | None = Field(default=None, ge=0, le=100_000)
    interval_minutes: int = Field(default=15, ge=1, le=240)
    persist: bool = True


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: int
    asset_code: str
    asset_kind: str
    setpoint_mw: float
    previous_mw: float
    delta_mw: float
    was_ramp_limited: bool
    marginal_cost: float
    reason: str


class DispatchOut(BaseModel):
    plan_id: int | None = None
    feeder_id: int
    interval_start: dt.datetime
    interval_minutes: int
    demand_mw: float
    generation_mw: float
    storage_net_mw: float
    curtailed_mw: float
    unserved_mw: float
    expected_cost: float
    reserve_mw: float
    reserve_required_mw: float
    reserve_short_mw: float
    status: str
    frequency_hz: float
    renewable_share: float
    marginal_price: float
    allocations: list[AllocationOut]
    notes: list[str]


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    feeder_id: int | None
    asset_id: int | None
    severity: Severity
    kind: str
    message: str
    evidence: str | None
    created_at: dt.datetime
    acknowledged_at: dt.datetime | None
    acknowledged_by: int | None


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
class FeederSummary(BaseModel):
    feeder_id: int
    code: str
    name: str
    region: str
    load_mw: float
    frequency_hz: float
    renewable_mw: float
    curtailed_mw: float
    capacity_mw: float
    headroom_mw: float
    utilisation: float
    open_alerts: int
    recorded_at: dt.datetime | None


class DashboardOut(BaseModel):
    generated_at: dt.datetime
    total_load_mw: float
    total_capacity_mw: float
    total_renewable_mw: float
    total_curtailed_mw: float
    average_frequency_hz: float
    renewable_share: float
    open_alerts: int
    critical_alerts: int
    feeders: list[FeederSummary]
