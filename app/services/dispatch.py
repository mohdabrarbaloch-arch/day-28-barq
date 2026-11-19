"""The dispatch solver.

Pure functions over dataclasses: no database, no I/O, no clock. That is
deliberate. Dispatch is the part of a grid platform that has to be right, and it
is only testable at this boundary.

What is modelled, and why each one is in here:

* **Merit order** - the cheapest available unit serves the next MW. Without it
  there is no cost story.
* **Ramp limits** - a unit can only move by ``ramp_mw_per_min * interval``. This
  is what makes a naive merit order wrong: the cheapest unit is often a 400 MW
  coal block that needs forty minutes to reach a target the interval does not
  have. Clamping it is what produces a genuine shortfall instead of a fictional
  one.
* **Minimum stable load** - below roughly 30-40% of rating a thermal unit has to
  come off, not throttle down. So a unit is either off or at least at
  ``min_stable_mw``. Starting a 60 MW-minimum unit to serve 5 MW is worse than
  leaving those 5 MW unserved only if something smaller exists; when nothing
  smaller exists, running at minimum and absorbing the over-generation is what a
  real system does, so there is a second pass for exactly that case.
* **Storage round-trip losses** - split symmetrically, ``sqrt(eta)`` on charge
  and ``sqrt(eta)`` on discharge, so 100 MWh through a 90% round-trip battery
  returns 90. Putting the whole loss on one side is a shortcut that makes a
  battery look better than it is in one direction.
* **N-1 reserve** - the requirement is the larger of a percentage of load and the
  biggest single unit, because the system must survive losing that unit.
* **Frequency** - derived from the residual imbalance through aggregate system
  stiffness, so the number on the dashboard is a consequence of the plan rather
  than an independent decoration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EPS = 1e-9

STORAGE_KIND = "battery"
RENEWABLE_KINDS = ("solar", "wind")
DISPATCHABLE_KINDS = ("thermal", "hydro")


class DispatchError(ValueError):
    """Raised when the inputs cannot describe a solvable interval."""


@dataclass(frozen=True)
class UnitSpec:
    """Everything the solver needs to know about one asset."""

    code: str
    kind: str
    capacity_mw: float
    min_stable_mw: float
    ramp_mw_per_min: float
    marginal_cost: float
    previous_mw: float = 0.0
    is_available: bool = True

    # Renewables: what the weather says is available this interval. Falls back
    # to nameplate capacity when unset.
    potential_mw: float | None = None

    # Storage only.
    storage_capacity_mwh: float | None = None
    max_charge_mw: float | None = None
    max_discharge_mw: float | None = None
    round_trip_efficiency: float | None = None
    state_of_charge_mwh: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("thermal", "hydro", "solar", "wind", "battery"):
            raise DispatchError(f"Unknown asset kind: {self.kind!r}")
        if self.capacity_mw < 0 or self.ramp_mw_per_min < 0:
            raise DispatchError(f"{self.code}: capacity and ramp must be >= 0")
        if self.min_stable_mw > self.capacity_mw + EPS:
            raise DispatchError(
                f"{self.code}: min_stable_mw ({self.min_stable_mw}) exceeds "
                f"capacity ({self.capacity_mw})"
            )
        if self.previous_mw < -EPS:
            raise DispatchError(f"{self.code}: previous_mw must be >= 0")
        if self.is_storage:
            eta = self.round_trip_efficiency
            if eta is not None and not (0.0 < eta <= 1.0):
                raise DispatchError(f"{self.code}: round_trip_efficiency must be in (0, 1]")

    @property
    def is_storage(self) -> bool:
        return self.kind == STORAGE_KIND

    @property
    def is_renewable(self) -> bool:
        return self.kind in RENEWABLE_KINDS

    @property
    def is_dispatchable(self) -> bool:
        return self.kind in DISPATCHABLE_KINDS


@dataclass
class UnitPlan:
    """One unit's setpoint and the reasoning behind it."""

    code: str
    kind: str
    setpoint_mw: float
    previous_mw: float
    marginal_cost: float
    reason: str
    ramp_limited: bool = False
    curtailed_mw: float = 0.0
    energy_delta_mwh: float = 0.0
    capacity_mw: float = 0.0

    @property
    def delta_mw(self) -> float:
        return self.setpoint_mw - self.previous_mw

    @property
    def headroom_mw(self) -> float:
        return max(0.0, self.capacity_mw - self.setpoint_mw)


@dataclass
class DispatchResult:
    interval_minutes: int
    demand_mw: float
    generation_mw: float
    storage_net_mw: float
    curtailed_mw: float
    unserved_mw: float
    expected_cost: float
    reserve_mw: float
    reserve_required_mw: float
    status: str
    frequency_hz: float
    units: list[UnitPlan]
    notes: list[str] = field(default_factory=list)

    @property
    def served_mw(self) -> float:
        return max(0.0, self.demand_mw - self.unserved_mw)

    @property
    def reserve_short_mw(self) -> float:
        return max(0.0, self.reserve_required_mw - self.reserve_mw)

    @property
    def renewable_mw(self) -> float:
        return sum(u.setpoint_mw for u in self.units if u.kind in RENEWABLE_KINDS)

    @property
    def renewable_share(self) -> float:
        if self.generation_mw <= EPS:
            return 0.0
        return self.renewable_mw / self.generation_mw

    @property
    def marginal_price(self) -> float:
        """Cost of the most expensive unit actually running."""
        running = [u.marginal_cost for u in self.units if u.setpoint_mw > EPS]
        return max(running) if running else 0.0


def _ramp_budget(spec: UnitSpec, interval_minutes: int) -> float:
    return spec.ramp_mw_per_min * interval_minutes


def _clamp_to_ramp(spec: UnitSpec, target: float, interval_minutes: int) -> tuple[float, bool]:
    """Limit a target to what the unit can reach in this interval."""
    budget = _ramp_budget(spec, interval_minutes)
    upper = spec.previous_mw + budget
    lower = max(0.0, spec.previous_mw - budget)
    if target > upper + EPS:
        return upper, True
    if target < lower - EPS:
        return lower, True
    return target, False


def _storage_available_discharge(spec: UnitSpec, interval_hours: float) -> float:
    """MW the battery can deliver for the whole interval."""
    if not spec.is_storage or interval_hours <= 0:
        return 0.0
    eta = math.sqrt(spec.round_trip_efficiency or 1.0)
    soc = spec.state_of_charge_mwh or 0.0
    limit_by_energy = (soc * eta) / interval_hours
    limit_by_power = spec.max_discharge_mw or spec.capacity_mw
    return max(0.0, min(limit_by_energy, limit_by_power))


def _storage_available_charge(spec: UnitSpec, interval_hours: float) -> float:
    """MW the battery can absorb for the whole interval."""
    if not spec.is_storage or interval_hours <= 0:
        return 0.0
    eta = math.sqrt(spec.round_trip_efficiency or 1.0)
    capacity = spec.storage_capacity_mwh or 0.0
    soc = spec.state_of_charge_mwh or 0.0
    headroom_mwh = max(0.0, capacity - soc)
    limit_by_energy = headroom_mwh / (eta * interval_hours)
    limit_by_power = spec.max_charge_mw or spec.capacity_mw
    return max(0.0, min(limit_by_energy, limit_by_power))


def solve_dispatch(
    units: list[UnitSpec],
    demand_mw: float,
    *,
    interval_minutes: int = 15,
    reserve_ratio: float = 0.08,
    reserve_window_minutes: float = 10.0,
    nominal_frequency_hz: float = 50.0,
    grid_stiffness_mw_per_hz: float = 550.0,
) -> DispatchResult:
    """Solve one dispatch interval.

    Order of operations, which is the real operational order:

    1. Must-run renewables at their available output.
    2. If renewables alone exceed demand, curtail them.
    3. Otherwise discharge storage up to its energy-limited maximum.
    4. Serve the remainder from dispatchable units in merit order, respecting
       ramp and minimum stable load.
    5. If a shortfall remains, absorb it in storage if any is left, then report
       it as unserved.
    6. Compute reserve, cost and frequency from the resulting plan.
    """
    if demand_mw < -EPS:
        raise DispatchError("demand_mw must be >= 0")
    if interval_minutes <= 0:
        raise DispatchError("interval_minutes must be > 0")
    if not units:
        raise DispatchError("At least one unit is required")
    if not (0.0 <= reserve_ratio <= 1.0):
        raise DispatchError("reserve_ratio must be between 0 and 1")
    if reserve_window_minutes <= 0:
        raise DispatchError("reserve_window_minutes must be > 0")

    interval_hours = interval_minutes / 60.0
    notes: list[str] = []
    plans: dict[str, UnitPlan] = {}

    available = [u for u in units if u.is_available]
    for u in units:
        if not u.is_available:
            plans[u.code] = UnitPlan(
                code=u.code,
                kind=u.kind,
                setpoint_mw=0.0,
                previous_mw=u.previous_mw,
                marginal_cost=u.marginal_cost,
                reason="Marked unavailable, held at zero.",
                capacity_mw=u.capacity_mw,
            )

    # --- 1. must-run renewables ------------------------------------------
    renewable_available = 0.0
    renewable_units: list[tuple[UnitSpec, UnitPlan]] = []
    for u in available:
        if not u.is_renewable:
            continue
        potential = u.potential_mw if u.potential_mw is not None else u.capacity_mw
        deliverable = max(0.0, min(potential, u.capacity_mw))
        renewable_available += deliverable
        renewable_units.append(
            (
                u,
                UnitPlan(
                    code=u.code,
                    kind=u.kind,
                    setpoint_mw=deliverable,
                    previous_mw=u.previous_mw,
                    marginal_cost=u.marginal_cost,
                    reason=(
                        f"Must-run at forecast output {deliverable:.1f} MW "
                        f"(available {potential:.1f}, rating {u.capacity_mw:.1f})."
                    ),
                    capacity_mw=u.capacity_mw,
                ),
            )
        )

    renewables_served = 0.0
    for _spec, plan in renewable_units:
        plans[plan.code] = plan
        renewables_served += plan.setpoint_mw

    # --- 2. storage absorbs the surplus BEFORE curtailment ---------------
    # Order matters here and it is an easy thing to get backwards. Curtailing
    # first and charging afterwards throws away energy that a battery could have
    # taken, so the app would report curtailment on a system with empty
    # batteries - a plausible-looking number that no operator would accept.
    storage_net_mw = 0.0
    storage_units = sorted(
        [u for u in available if u.is_storage], key=lambda u: (u.marginal_cost, u.code)
    )

    surplus = max(0.0, renewables_served - demand_mw)

    # Only charge when renewables alone overshoot demand. Topping the battery up
    # from the thermal fleet is a real strategy, but it is a decision about price
    # spread over the next few hours, not something a single-interval merit-order
    # solve can justify - so this solver does not do it and says so in the notes.
    if surplus > EPS and storage_units:
        for u in storage_units:
            if surplus <= EPS:
                break
            headroom = _storage_available_charge(u, interval_hours)
            if headroom <= EPS:
                plans[u.code] = UnitPlan(
                    code=u.code,
                    kind=u.kind,
                    setpoint_mw=0.0,
                    previous_mw=u.previous_mw,
                    marginal_cost=u.marginal_cost,
                    reason=(
                        f"Full: {u.state_of_charge_mwh or 0:.0f} of "
                        f"{u.storage_capacity_mwh or 0:.0f} MWh stored, no "
                        "headroom to absorb the surplus."
                    ),
                    capacity_mw=u.capacity_mw,
                )
                continue
            take = min(headroom, surplus)
            eta = math.sqrt(u.round_trip_efficiency or 1.0)
            stored = take * eta * interval_hours
            plans[u.code] = UnitPlan(
                code=u.code,
                kind=u.kind,
                setpoint_mw=-take,
                previous_mw=u.previous_mw,
                marginal_cost=u.marginal_cost,
                reason=(
                    f"Charging {take:.1f} MW from surplus renewable output; "
                    f"{stored:.1f} MWh stored after a "
                    f"{(1 - eta) * 100:.1f}% charge loss."
                ),
                energy_delta_mwh=stored,
                capacity_mw=u.capacity_mw,
            )
            storage_net_mw -= take
            surplus -= take

    # --- 3. curtail only what the batteries could not absorb -------------
    curtailed_total = 0.0
    if surplus > EPS:
        excess = surplus
        # Cut proportionally across the farms rather than taking it all from one
        # site: operators spread curtailment to keep every unit above its
        # technical minimum and to share the commercial impact fairly.
        for _spec, plan in renewable_units:
            share = plan.setpoint_mw / renewables_served if renewables_served else 0.0
            cut = min(plan.setpoint_mw, excess * share)
            if cut <= EPS:
                continue
            original = plan.setpoint_mw
            plan.setpoint_mw -= cut
            plan.curtailed_mw = cut
            plan.reason = (
                f"Curtailed {cut:.1f} MW of {original:.1f} MW: renewables alone "
                f"({renewable_available:.1f} MW) exceeded demand "
                f"({demand_mw:.1f} MW) and the batteries could not absorb the rest."
            )
            curtailed_total += cut
        renewables_served -= curtailed_total
        notes.append(
            f"Renewable output exceeded demand by {excess:.1f} MW beyond what "
            f"storage could take, so {curtailed_total:.1f} MW was curtailed."
        )

    shortfall = max(0.0, demand_mw - renewables_served - storage_net_mw)

    # --- 4. storage discharges into any remaining shortfall --------------
    if shortfall > EPS and storage_units and storage_net_mw >= -EPS:
        for u in storage_units:
            if shortfall <= EPS:
                break
            deliverable = _storage_available_discharge(u, interval_hours)
            if deliverable <= EPS:
                plans[u.code] = UnitPlan(
                    code=u.code,
                    kind=u.kind,
                    setpoint_mw=0.0,
                    previous_mw=u.previous_mw,
                    marginal_cost=u.marginal_cost,
                    reason=(
                        f"Empty: {u.state_of_charge_mwh or 0:.0f} MWh stored, "
                        "nothing deliverable this interval."
                    ),
                    capacity_mw=u.capacity_mw,
                )
                continue
            give = min(deliverable, shortfall)
            eta = math.sqrt(u.round_trip_efficiency or 1.0)
            drawn = (give * interval_hours) / eta
            plans[u.code] = UnitPlan(
                code=u.code,
                kind=u.kind,
                setpoint_mw=give,
                previous_mw=u.previous_mw,
                marginal_cost=u.marginal_cost,
                reason=(
                    f"Discharging {give:.1f} MW to cover the deficit; draws "
                    f"{drawn:.1f} MWh of the {u.state_of_charge_mwh or 0:.0f} MWh "
                    f"stored ({(1 - eta) * 100:.1f}% discharge loss)."
                ),
                energy_delta_mwh=-drawn,
                capacity_mw=u.capacity_mw,
            )
            storage_net_mw += give
            shortfall -= give

    for u in storage_units:
        if u.code not in plans:
            plans[u.code] = UnitPlan(
                code=u.code,
                kind=u.kind,
                setpoint_mw=0.0,
                previous_mw=u.previous_mw,
                marginal_cost=u.marginal_cost,
                reason=(
                    f"Idle: {u.state_of_charge_mwh or 0:.0f} MWh stored, but "
                    "storage only runs against a net surplus or deficit."
                ),
                capacity_mw=u.capacity_mw,
            )

    # --- 4. dispatchable units in merit order ----------------------------
    need = max(0.0, demand_mw - renewables_served - storage_net_mw)
    dispatchable = sorted(
        [u for u in available if u.is_dispatchable],
        key=lambda u: (u.marginal_cost, u.code),
    )
    # Each unit has an achievable band this interval, and the band is not
    # symmetric. A unit already running cannot drop below
    # max(min_stable, previous - budget): that floor is *forced* output, and it
    # is the reason a system with plenty of cheap capacity can still show
    # over-generation. Modelling only the upward direction - which is the
    # obvious way to write this and the way it was written first - lets a 300 MW
    # coal unit teleport to zero in a fifteen-minute interval, which is not a
    # thing a coal unit can do.
    bands: dict[str, tuple[float, float, bool]] = {}
    for unit in dispatchable:
        budget = _ramp_budget(unit, interval_minutes)
        was_on = unit.previous_mw > EPS
        if was_on:
            floor = min(max(unit.min_stable_mw, unit.previous_mw - budget), unit.capacity_mw)
            ceiling = min(unit.capacity_mw, unit.previous_mw + budget)
        else:
            floor = 0.0
            ceiling = min(unit.capacity_mw, budget)
        bands[unit.code] = (floor, ceiling, was_on)

    setpoints: dict[str, float] = {u.code: bands[u.code][0] for u in dispatchable}
    forced = sum(bands[u.code][0] for u in dispatchable)
    remaining = need - forced

    # Track whether a unit was asked for more than its band allowed. Comparing
    # the final setpoint against the ceiling cannot detect this: a unit clamped
    # to exactly its ceiling lands on the boundary and looks unconstrained.
    clamped: set[str] = set()

    if remaining > EPS:
        for unit in dispatchable:
            if remaining <= EPS:
                break
            floor, ceiling, was_on = bands[unit.code]
            headroom = ceiling - floor
            if headroom <= EPS:
                if remaining > EPS:
                    clamped.add(unit.code)
                continue
            # Starting a cold unit means committing it to at least its minimum
            # stable load. If the gap is smaller than that, this unit is the
            # wrong tool and the overshoot pass below decides instead.
            if not was_on and unit.min_stable_mw > EPS and remaining < unit.min_stable_mw - EPS:
                continue
            take = min(headroom, remaining)
            if remaining > headroom + EPS:
                clamped.add(unit.code)
            setpoints[unit.code] = floor + take
            remaining -= take

    # Min-stable overshoot pass. The gap is smaller than any remaining unit's
    # floor, so the cheapest one is committed at its minimum and the excess is
    # absorbed as genuine over-generation - which is what an operator does,
    # rather than shedding load over a rounding difference.
    if remaining > EPS:
        for unit in dispatchable:
            floor, ceiling, was_on = bands[unit.code]
            if was_on or unit.min_stable_mw <= EPS:
                continue
            if ceiling < unit.min_stable_mw - EPS:
                continue
            setpoints[unit.code] = unit.min_stable_mw
            notes.append(
                f"No unit could serve {remaining:.1f} MW without breaking minimum "
                f"stable load, so {unit.code} was committed at its "
                f"{unit.min_stable_mw:.0f} MW floor. The excess is real "
                "over-generation, not a modelling artefact."
            )
            remaining = 0.0
            break

    for unit in dispatchable:
        floor, ceiling, was_on = bands[unit.code]
        target = setpoints[unit.code]
        ramp_limited = unit.code in clamped
        if target > EPS:
            reason = (
                f"Merit position: PKR {unit.marginal_cost:,.0f}/MWh. Set to " f"{target:.1f} MW."
            )
            if ramp_limited:
                reason += (
                    f" Ramp-limited: {unit.ramp_mw_per_min:.1f} MW/min allows "
                    f"{_ramp_budget(unit, interval_minutes):.0f} MW of movement "
                    f"this interval, so the target was clamped."
                )
            elif was_on and target < unit.previous_mw - EPS:
                reason += (
                    f" Ramped down from {unit.previous_mw:.1f} MW as far as the "
                    f"{unit.ramp_mw_per_min:.1f} MW/min limit allows."
                )
        elif was_on:
            reason = (
                f"Ramped down to zero from {unit.previous_mw:.1f} MW: cheaper "
                f"capacity covers demand and the ramp limit allows it to come off."
            )
        else:
            reason = (
                "Not needed: cheaper capacity covered demand first "
                f"(this unit costs PKR {unit.marginal_cost:,.0f}/MWh)."
            )
        plans[unit.code] = UnitPlan(
            code=unit.code,
            kind=unit.kind,
            setpoint_mw=target,
            previous_mw=unit.previous_mw,
            marginal_cost=unit.marginal_cost,
            reason=reason,
            ramp_limited=ramp_limited,
            capacity_mw=unit.capacity_mw,
        )

    # --- 5. totals -------------------------------------------------------
    generation_mw = (
        sum(p.setpoint_mw for p in plans.values() if p.kind in DISPATCHABLE_KINDS)
        + renewables_served
    )
    delivered = generation_mw + storage_net_mw
    unserved = max(0.0, demand_mw - delivered)
    over_generation = max(0.0, delivered - demand_mw)

    if unserved > EPS:
        notes.append(
            f"{unserved:.1f} MW went unserved: the remaining units were at their "
            "ramp limits, offline, or still too expensive to commit."
        )
    if over_generation > EPS and not any("stable load" in n for n in notes):
        notes.append(f"{over_generation:.1f} MW of over-generation.")

    # --- 6. reserve ------------------------------------------------------
    # Spinning reserve is headroom on units that are actually running, and only
    # as much of it as they can reach inside the reserve window. Counting
    # nameplate minus setpoint for every available unit - which is the obvious
    # way to write this, and the way it was written first - credits a cold coal
    # block sitting at 0 MW with 400 MW of "reserve" it cannot deliver, because
    # it is not spinning and would need hours to start.
    reserve = 0.0
    for unit in available:
        if unit.is_storage:
            continue
        plan = plans.get(unit.code)
        setpoint = plan.setpoint_mw if plan else 0.0
        if setpoint <= EPS:
            continue  # not running, so not spinning
        headroom = max(0.0, unit.capacity_mw - setpoint)
        reachable = unit.ramp_mw_per_min * reserve_window_minutes
        reserve += min(headroom, reachable)

    largest_unit = max((u.capacity_mw for u in available), default=0.0)
    reserve_required = max(demand_mw * reserve_ratio, largest_unit)

    # --- 7. cost ---------------------------------------------------------
    cost = 0.0
    for unit in units:
        plan = plans.get(unit.code)
        if plan is None or plan.setpoint_mw <= 0:
            continue
        cost += plan.setpoint_mw * unit.marginal_cost * interval_hours

    # --- 8. frequency from the residual imbalance ------------------------
    imbalance = -unserved if unserved > EPS else over_generation
    # Over-generation pushes frequency up, under-generation pulls it down.
    if over_generation > EPS and unserved <= EPS:
        imbalance = over_generation
    stiffness = max(1.0, grid_stiffness_mw_per_hz)
    frequency = nominal_frequency_hz + (imbalance / stiffness)
    frequency = max(45.0, min(55.0, frequency))

    if unserved > 0.5:
        status = "deficit"
    elif over_generation > 0.5:
        status = "surplus"
    else:
        status = "feasible"

    ordered = sorted(plans.values(), key=lambda p: (-p.setpoint_mw, p.marginal_cost, p.code))

    return DispatchResult(
        interval_minutes=interval_minutes,
        demand_mw=demand_mw,
        generation_mw=generation_mw,
        storage_net_mw=storage_net_mw,
        curtailed_mw=curtailed_total,
        unserved_mw=unserved,
        expected_cost=cost,
        reserve_mw=reserve,
        reserve_required_mw=reserve_required,
        status=status,
        frequency_hz=frequency,
        units=ordered,
        notes=notes,
    )


def reserve_requirement_mw(
    units: list[UnitSpec], demand_mw: float, reserve_ratio: float = 0.08
) -> float:
    """The N-1 reserve requirement, exposed so it can be charted on its own."""
    available = [u for u in units if u.is_available]
    largest = max((u.capacity_mw for u in available), default=0.0)
    return max(demand_mw * reserve_ratio, largest)
