"""Grid entities: feeders, assets, telemetry, dispatch plans and alerts.

The shape follows the physics. A dispatch plan owns one row per asset, and each
of those rows carries the reasoning that produced its setpoint, so a plan can be
explained line by line afterwards instead of being an opaque total.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.types import UtcDateTime, utcnow

# thermal  - coal/gas, dispatchable, has a fuel cost and a minimum stable load
# hydro    - fast ramp, cheap, limited by water
# solar    - must-run when the sun is up, zero marginal cost, curtailable
# wind     - must-run, zero marginal cost, curtailable, larger forecast error
# battery  - storage, either sign, has round-trip losses
ASSET_KINDS = ("thermal", "hydro", "solar", "wind", "battery")
RENEWABLE_KINDS = ("solar", "wind")

ALERT_SEVERITIES = ("info", "warning", "critical")
ALERT_KINDS = (
    "frequency_low",
    "frequency_high",
    "reserve_shortfall",
    "unserved_energy",
    "curtailment",
    "asset_trip",
    "forecast_error",
)
PLAN_STATUSES = ("feasible", "deficit", "surplus")


class Feeder(Base):
    """A distribution zone with its own load profile."""

    __tablename__ = "feeders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    peak_load_mw: Mapped[float] = mapped_column(Float, nullable=False)
    # Share of load that is industrial vs residential, 0-1. Industrial load is
    # flatter through the day, which is why two feeders with the same peak have
    # different ramp requirements.
    industrial_share: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    assets: Mapped[list[EnergyAsset]] = relationship(back_populates="feeder")
    readings: Mapped[list[TelemetryReading]] = relationship(back_populates="feeder")

    __table_args__ = (Index("ix_feeders_region_active", "region", "is_active"),)


class EnergyAsset(Base):
    """A generating unit or a storage system attached to a feeder."""

    __tablename__ = "energy_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    feeder_id: Mapped[int] = mapped_column(
        ForeignKey("feeders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)

    capacity_mw: Mapped[float] = mapped_column(Float, nullable=False)
    # Minimum stable generation. A coal unit cannot run at 5% of rating - below
    # this it has to be off entirely, so min-load is a real constraint.
    min_stable_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # MW per minute. A 400 MW coal unit ramps at roughly 4-8 MW/min, hydro at
    # 40-80. This is the constraint that makes a naive merit order wrong.
    ramp_mw_per_min: Mapped[float] = mapped_column(Float, nullable=False)
    marginal_cost: Mapped[float] = mapped_column(Float, nullable=False)

    # --- storage-only fields, null for generators -------------------------
    storage_capacity_mwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_charge_mw: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_discharge_mw: Mapped[float | None] = mapped_column(Float, nullable=True)
    round_trip_efficiency: Mapped[float | None] = mapped_column(Float, nullable=True)
    state_of_charge_mwh: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_renewable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    feeder: Mapped[Feeder] = relationship(back_populates="assets")
    allocations: Mapped[list[DispatchAllocation]] = relationship(back_populates="asset")

    @property
    def is_storage(self) -> bool:
        return self.kind == "battery"

    @property
    def available_capacity_mw(self) -> float:
        """What this unit can actually deliver right now."""
        return self.capacity_mw if self.is_available else 0.0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Asset {self.code} {self.kind} {self.capacity_mw}MW>"


class TelemetryReading(Base):
    """One snapshot of the grid, normally every 15 minutes."""

    __tablename__ = "telemetry_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feeder_id: Mapped[int] = mapped_column(
        ForeignKey("feeders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)

    load_mw: Mapped[float] = mapped_column(Float, nullable=False)
    frequency_hz: Mapped[float] = mapped_column(Float, nullable=False)
    # Renewable output actually delivered (after any curtailment).
    renewable_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # What the renewables could have produced given the weather.
    renewable_potential_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Net flow into the feeder from the transmission system; negative = export.
    import_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    feeder: Mapped[Feeder] = relationship(back_populates="readings")

    @property
    def curtailed_mw(self) -> float:
        return max(0.0, self.renewable_potential_mw - self.renewable_mw)

    __table_args__ = (
        Index("ix_telemetry_feeder_time", "feeder_id", "recorded_at"),
        Index("ix_telemetry_time_desc", "recorded_at"),
    )


class DispatchPlan(Base):
    """The output of one dispatch solve: the whole plan plus its totals."""

    __tablename__ = "dispatch_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feeder_id: Mapped[int] = mapped_column(
        ForeignKey("feeders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    interval_start: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)

    demand_mw: Mapped[float] = mapped_column(Float, nullable=False)
    generation_mw: Mapped[float] = mapped_column(Float, nullable=False)
    storage_net_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    curtailed_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unserved_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    expected_cost: Mapped[float] = mapped_column(Float, nullable=False)
    reserve_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reserve_required_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    frequency_hz: Mapped[float] = mapped_column(Float, nullable=False)

    allocations: Mapped[list[DispatchAllocation]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_plans_feeder_created", "feeder_id", "created_at"),)

    @property
    def reserve_short_mw(self) -> float:
        return max(0.0, self.reserve_required_mw - self.reserve_mw)

    @property
    def renewable_share(self) -> float:
        if self.generation_mw <= 0:
            return 0.0
        delivered = self.generation_mw - self.curtailed_mw
        return delivered / self.generation_mw if self.generation_mw else 0.0


class DispatchAllocation(Base):
    """One asset's setpoint within a plan, with the reason behind it."""

    __tablename__ = "dispatch_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("dispatch_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("energy_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    setpoint_mw: Mapped[float] = mapped_column(Float, nullable=False)
    previous_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Actual delta applied after clamping to the ramp rate. Kept separate from
    # (setpoint - previous) because those differ when a unit was ramp-limited,
    # and that difference is the whole point of tracking it.
    delivered_delta_mw: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    was_ramp_limited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    marginal_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Human-readable account of why this unit got this number.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    plan: Mapped[DispatchPlan] = relationship(back_populates="allocations")
    asset: Mapped[EnergyAsset] = relationship(back_populates="allocations", lazy="joined")


class GridAlert(Base):
    """Something the operator needs to know about."""

    __tablename__ = "grid_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feeder_id: Mapped[int | None] = mapped_column(
        ForeignKey("feeders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("energy_assets.id", ondelete="SET NULL"), nullable=True
    )
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # The measured evidence, so the alert is checkable rather than a claim.
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False, index=True
    )
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_alerts_open", "acknowledged_at", "severity"),
        Index("ix_alerts_feeder_time", "feeder_id", "created_at"),
    )

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None
