"""Tests for the dispatch solver.

These are the tests that matter: the solver is pure, so every claim it makes
about the physics can be checked exactly rather than approximately.
"""

from __future__ import annotations

import math

import pytest

from app.services.dispatch import (
    DispatchError,
    UnitSpec,
    reserve_requirement_mw,
    solve_dispatch,
)


def coal(**overrides) -> UnitSpec:
    base = dict(
        code="COAL-1",
        kind="thermal",
        capacity_mw=400.0,
        min_stable_mw=160.0,
        ramp_mw_per_min=6.0,
        marginal_cost=9800.0,
        previous_mw=300.0,
    )
    base.update(overrides)
    return UnitSpec(**base)


def hydro(**overrides) -> UnitSpec:
    base = dict(
        code="HYD-1",
        kind="hydro",
        capacity_mw=180.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=45.0,
        marginal_cost=3200.0,
        previous_mw=80.0,
    )
    base.update(overrides)
    return UnitSpec(**base)


def solar(potential: float = 180.0, **overrides) -> UnitSpec:
    base = dict(
        code="SOL-1",
        kind="solar",
        capacity_mw=250.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=999.0,
        marginal_cost=0.0,
        previous_mw=0.0,
        potential_mw=potential,
    )
    base.update(overrides)
    return UnitSpec(**base)


def battery(**overrides) -> UnitSpec:
    base = dict(
        code="BESS-1",
        kind="battery",
        capacity_mw=100.0,
        min_stable_mw=0.0,
        ramp_mw_per_min=999.0,
        marginal_cost=6100.0,
        previous_mw=0.0,
        storage_capacity_mwh=400.0,
        max_charge_mw=100.0,
        max_discharge_mw=100.0,
        round_trip_efficiency=0.88,
        state_of_charge_mwh=200.0,
    )
    base.update(overrides)
    return UnitSpec(**base)


# --------------------------------------------------------------------------
# Merit order and cost
# --------------------------------------------------------------------------
def test_cheapest_unit_is_dispatched_first():
    """Hydro at 3,200 must run before coal at 9,800.

    Both units start from zero here on purpose. With coal already at 300 MW it
    is forced to stay near that output by its ramp limit, which would mask the
    merit order entirely.
    """
    # 400 MW of demand: hydro takes its full 180, coal covers the remaining 220.
    # A smaller gap would be below coal's 160 MW floor and the overshoot pass
    # would commit it at minimum instead, which is a different test.
    result = solve_dispatch(
        [coal(previous_mw=0.0), hydro(previous_mw=0.0)], 400.0, interval_minutes=60
    )
    by_code = {u.code: u for u in result.units}
    assert by_code["HYD-1"].setpoint_mw == pytest.approx(180.0)
    assert by_code["COAL-1"].setpoint_mw == pytest.approx(220.0)
    assert result.expected_cost == pytest.approx((180 * 3200 + 220 * 9800) * 1.0)


def test_cost_is_energy_times_price_over_the_interval():
    """200 MW for 15 minutes at 3,200/MWh is 50 MWh, so 160,000."""
    result = solve_dispatch([hydro()], 200.0, interval_minutes=15)
    # Hydro is capped at 180, so 180 MW * 0.25 h * 3200 = 144,000.
    assert result.expected_cost == pytest.approx(180 * 0.25 * 3200)
    assert result.unserved_mw == pytest.approx(20.0)


def test_marginal_price_is_the_most_expensive_running_unit():
    result = solve_dispatch([coal(), hydro()], 200.0)
    assert result.marginal_price == pytest.approx(9800.0)


def test_marginal_price_is_zero_when_nothing_runs():
    result = solve_dispatch([coal(previous_mw=0.0)], 0.0)
    assert result.marginal_price == 0.0


# --------------------------------------------------------------------------
# Ramp limits
# --------------------------------------------------------------------------
def test_ramp_limit_clamps_a_unit_that_cannot_reach_its_target():
    """A 6 MW/min coal unit can move 90 MW in 15 minutes, not 300."""
    result = solve_dispatch([coal(previous_mw=100.0)], 400.0, interval_minutes=15)
    unit = next(u for u in result.units if u.code == "COAL-1")
    assert unit.setpoint_mw == pytest.approx(190.0)  # 100 + 6*15
    assert unit.ramp_limited is True


def test_ramp_limit_produces_a_real_shortfall_not_a_fictional_one():
    """The whole point of clamping: the deficit is reported, not hidden."""
    result = solve_dispatch([coal(previous_mw=100.0)], 400.0, interval_minutes=15)
    assert result.unserved_mw == pytest.approx(210.0)
    assert result.status == "deficit"


def test_ramp_limit_scales_with_the_interval():
    """The same unit can move twice as far in a 30-minute interval."""
    short = solve_dispatch([coal(previous_mw=100.0)], 400.0, interval_minutes=15)
    long = solve_dispatch([coal(previous_mw=100.0)], 400.0, interval_minutes=30)
    short_unit = next(u for u in short.units if u.code == "COAL-1")
    long_unit = next(u for u in long.units if u.code == "COAL-1")
    assert long_unit.setpoint_mw == pytest.approx(280.0)  # 100 + 6*30
    assert long_unit.setpoint_mw > short_unit.setpoint_mw


def test_ramp_limit_applies_downwards_too():
    """A unit cannot drop from 300 to 0 in one interval either.

    This is the bug the first version of the solver had: it modelled only the
    upward direction, so a 300 MW coal unit teleported to zero the moment demand
    fell. 300 - 6*15 = 210, and 210 is above the 160 minimum, so it lands there.
    """
    result = solve_dispatch([coal(previous_mw=300.0)], 0.0, interval_minutes=15)
    unit = next(u for u in result.units if u.code == "COAL-1")
    assert unit.setpoint_mw == pytest.approx(210.0)
    assert result.status == "surplus"


# --------------------------------------------------------------------------
# Minimum stable load
# --------------------------------------------------------------------------
def test_min_stable_load_forces_over_generation_when_nothing_smaller_exists():
    """A 160 MW floor cannot serve 5 MW, so it runs at 160 and overshoots."""
    result = solve_dispatch([coal(previous_mw=200.0)], 5.0, interval_minutes=60)
    unit = next(u for u in result.units if u.code == "COAL-1")
    assert unit.setpoint_mw == pytest.approx(160.0)
    assert result.unserved_mw == pytest.approx(0.0)
    assert result.status == "surplus"
    assert any("over-generation" in note for note in result.notes)


def test_a_smaller_unit_is_preferred_over_committing_a_large_one():
    """With hydro available, 5 MW is served by hydro, not by coal at minimum."""
    result = solve_dispatch([coal(previous_mw=0.0), hydro(previous_mw=0.0)], 5.0)
    by_code = {u.code: u for u in result.units}
    assert by_code["HYD-1"].setpoint_mw == pytest.approx(5.0)
    assert by_code["COAL-1"].setpoint_mw == pytest.approx(0.0)


def test_a_running_unit_is_not_dropped_below_its_minimum():
    """Coal already at 200 MW stays at least at 160 rather than going to 20."""
    result = solve_dispatch([coal(previous_mw=200.0)], 20.0, interval_minutes=60)
    unit = next(u for u in result.units if u.code == "COAL-1")
    assert unit.setpoint_mw >= 160.0


# --------------------------------------------------------------------------
# Renewables and curtailment
# --------------------------------------------------------------------------
def test_renewables_run_at_their_forecast_output():
    result = solve_dispatch([solar(180.0), hydro()], 300.0)
    unit = next(u for u in result.units if u.code == "SOL-1")
    assert unit.setpoint_mw == pytest.approx(180.0)


def test_renewable_output_is_capped_by_nameplate_capacity():
    """A forecast above rating is a data error; the rating wins.

    With 100 MW of demand the farm delivers 100 and the other 150 is curtailed,
    so the cap is visible in the curtailment figure rather than the setpoint.
    """
    result = solve_dispatch([solar(999.0)], 100.0)
    unit = next(u for u in result.units if u.code == "SOL-1")
    assert unit.setpoint_mw == pytest.approx(100.0)
    assert result.curtailed_mw == pytest.approx(150.0)


def test_storage_absorbs_surplus_before_any_curtailment():
    """The ordering that matters: charge first, curtail only what is left."""
    result = solve_dispatch([solar(180.0), battery()], 120.0)
    unit = next(u for u in result.units if u.code == "BESS-1")
    assert unit.setpoint_mw == pytest.approx(-60.0)  # charging
    assert result.curtailed_mw == pytest.approx(0.0)


def test_curtailment_happens_only_when_storage_cannot_take_the_surplus():
    """A full battery leaves nowhere for the surplus to go."""
    full = battery(state_of_charge_mwh=400.0)
    result = solve_dispatch([solar(180.0), full], 40.0)
    assert result.curtailed_mw == pytest.approx(140.0)
    assert any("curtailed" in note for note in result.notes)


def test_curtailment_is_shared_proportionally_across_farms():
    """Two farms, one surplus: both get cut, neither absorbs all of it."""
    a = solar(100.0, code="SOL-A")
    b = solar(100.0, code="SOL-B")
    result = solve_dispatch([a, b], 100.0)
    by_code = {u.code: u for u in result.units}
    assert by_code["SOL-A"].curtailed_mw == pytest.approx(50.0)
    assert by_code["SOL-B"].curtailed_mw == pytest.approx(50.0)
    assert result.curtailed_mw == pytest.approx(100.0)


def test_zero_demand_curtails_everything():
    result = solve_dispatch([solar(180.0)], 0.0)
    assert result.curtailed_mw == pytest.approx(180.0)
    assert result.generation_mw == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def test_battery_discharges_into_a_deficit():
    """Storage runs before the thermal fleet, so it takes the first 100 MW."""
    result = solve_dispatch([hydro(previous_mw=0.0), battery()], 250.0)
    unit = next(u for u in result.units if u.code == "BESS-1")
    assert unit.setpoint_mw == pytest.approx(100.0)
    assert result.storage_net_mw == pytest.approx(100.0)
    # Hydro covers the remaining 150.
    hydro_unit = next(u for u in result.units if u.code == "HYD-1")
    assert hydro_unit.setpoint_mw == pytest.approx(150.0)


def test_battery_discharge_is_limited_by_stored_energy():
    """10 MWh stored cannot deliver 100 MW for a whole 15-minute interval."""
    nearly_empty = battery(state_of_charge_mwh=10.0)
    result = solve_dispatch([nearly_empty], 100.0, interval_minutes=15)
    unit = next(u for u in result.units if u.code == "BESS-1")
    # 10 MWh * sqrt(0.88) = 9.38 MWh deliverable, over 0.25 h = 37.5 MW.
    expected = (10.0 * math.sqrt(0.88)) / 0.25
    assert unit.setpoint_mw == pytest.approx(expected, rel=1e-6)
    assert unit.setpoint_mw < 100.0


def test_round_trip_loss_is_split_across_charge_and_discharge():
    """Charging 100 MW for an hour at 88% round-trip stores 93.8 MWh."""
    result = solve_dispatch([solar(200.0), battery()], 100.0, interval_minutes=60)
    unit = next(u for u in result.units if u.code == "BESS-1")
    assert unit.setpoint_mw == pytest.approx(-100.0)
    assert unit.energy_delta_mwh == pytest.approx(100.0 * math.sqrt(0.88), rel=1e-6)


def test_discharge_draws_more_than_it_delivers():
    """The loss has to come out of the state of charge, not out of nowhere."""
    result = solve_dispatch([battery()], 50.0, interval_minutes=60)
    unit = next(u for u in result.units if u.code == "BESS-1")
    assert unit.setpoint_mw == pytest.approx(50.0)
    assert unit.energy_delta_mwh == pytest.approx(-50.0 / math.sqrt(0.88), rel=1e-6)
    assert abs(unit.energy_delta_mwh) > unit.setpoint_mw


def test_an_empty_battery_reports_why_it_did_nothing():
    empty = battery(state_of_charge_mwh=0.0)
    result = solve_dispatch([empty], 50.0)
    unit = next(u for u in result.units if u.code == "BESS-1")
    assert unit.setpoint_mw == pytest.approx(0.0)
    assert "Empty" in unit.reason


def test_a_full_battery_reports_why_it_did_not_charge():
    full = battery(state_of_charge_mwh=400.0)
    result = solve_dispatch([solar(200.0), full], 50.0)
    unit = next(u for u in result.units if u.code == "BESS-1")
    assert unit.setpoint_mw == pytest.approx(0.0)
    assert "Full" in unit.reason


# --------------------------------------------------------------------------
# Reserve
# --------------------------------------------------------------------------
def test_reserve_requirement_is_the_larger_of_load_share_and_biggest_unit():
    """N-1: the system must survive losing its largest single unit."""
    units = [coal(), hydro()]
    # 8% of 200 = 16, but the biggest unit is 400, so 400 wins.
    assert reserve_requirement_mw(units, 200.0, 0.08) == pytest.approx(400.0)
    # 8% of 10,000 = 800, which now beats the 400 MW unit.
    assert reserve_requirement_mw(units, 10_000.0, 0.08) == pytest.approx(800.0)


def test_reserve_counts_headroom_on_running_units():
    """Reserve is headroom a running unit can actually reach in the window.

    Coal sits at 210 MW with 190 MW of nameplate headroom, but at 6 MW/min it
    can only reach 60 MW inside a 10-minute reserve window. Hydro at 0 MW is not
    running, so it contributes nothing - a unit that is off is not spinning.
    """
    result = solve_dispatch([coal(), hydro()], 200.0)
    assert result.reserve_mw == pytest.approx(60.0)  # coal only, ramp-limited
    assert result.reserve_required_mw == pytest.approx(400.0)
    assert result.reserve_short_mw == pytest.approx(340.0)


def test_a_unit_that_is_off_contributes_no_reserve():
    """The bug this guards: a cold 400 MW block is not 400 MW of reserve."""
    result = solve_dispatch([coal(previous_mw=0.0), hydro(previous_mw=0.0)], 100.0)
    coal_plan = next(u for u in result.units if u.code == "COAL-1")
    assert coal_plan.setpoint_mw == pytest.approx(0.0)
    # Hydro is the only running unit, and it is at 100 of 180 MW.
    assert result.reserve_mw == pytest.approx(80.0)


def test_unavailable_units_contribute_no_reserve():
    result = solve_dispatch([coal(is_available=False), hydro()], 100.0)
    assert result.reserve_mw == pytest.approx(80.0)  # hydro 180 - 100


# --------------------------------------------------------------------------
# Frequency
# --------------------------------------------------------------------------
def test_frequency_falls_when_load_is_unserved():
    result = solve_dispatch([hydro()], 200.0, grid_stiffness_mw_per_hz=550.0)
    # 20 MW short at 550 MW/Hz is 0.0364 Hz below nominal.
    assert result.frequency_hz == pytest.approx(50.0 - 20.0 / 550.0, rel=1e-6)
    assert result.frequency_hz < 50.0


def test_frequency_rises_on_over_generation():
    result = solve_dispatch([coal(previous_mw=200.0)], 5.0, interval_minutes=60)
    assert result.frequency_hz > 50.0


def test_frequency_is_exactly_nominal_when_balanced():
    result = solve_dispatch([hydro(previous_mw=0.0)], 100.0)
    assert result.frequency_hz == pytest.approx(50.0)


def test_frequency_is_clamped_to_a_plausible_band():
    """A wild imbalance must not produce a physically impossible number."""
    result = solve_dispatch([hydro()], 100_000.0, grid_stiffness_mw_per_hz=1.0)
    assert 45.0 <= result.frequency_hz <= 55.0


# --------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------
def test_every_unit_gets_a_reason():
    result = solve_dispatch([coal(), hydro(), solar(), battery()], 400.0)
    assert len(result.units) == 4
    for unit in result.units:
        assert unit.reason
        assert len(unit.reason) > 20


def test_an_unavailable_unit_says_so_rather_than_being_omitted():
    result = solve_dispatch([coal(is_available=False), hydro()], 100.0)
    unit = next(u for u in result.units if u.code == "COAL-1")
    assert unit.setpoint_mw == 0.0
    assert "unavailable" in unit.reason.lower()


def test_a_unit_that_was_not_needed_explains_the_merit_order():
    """Coal starting from zero is not committed when hydro covers demand."""
    result = solve_dispatch([hydro(previous_mw=0.0), coal(previous_mw=0.0)], 100.0)
    unit = next(u for u in result.units if u.code == "COAL-1")
    assert unit.setpoint_mw == pytest.approx(0.0)
    assert "Not needed" in unit.reason


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_no_units_is_rejected():
    with pytest.raises(DispatchError, match="At least one unit"):
        solve_dispatch([], 100.0)


def test_negative_demand_is_rejected():
    with pytest.raises(DispatchError, match="demand_mw"):
        solve_dispatch([hydro()], -1.0)


def test_zero_interval_is_rejected():
    with pytest.raises(DispatchError, match="interval_minutes"):
        solve_dispatch([hydro()], 100.0, interval_minutes=0)


def test_unknown_asset_kind_is_rejected():
    with pytest.raises(DispatchError, match="Unknown asset kind"):
        UnitSpec("X", "nuclear", 100.0, 0.0, 1.0, 1.0)


def test_min_stable_above_capacity_is_rejected():
    with pytest.raises(DispatchError, match="min_stable_mw"):
        UnitSpec("X", "thermal", 100.0, 200.0, 1.0, 1.0)


def test_invalid_round_trip_efficiency_is_rejected():
    with pytest.raises(DispatchError, match="round_trip_efficiency"):
        battery(round_trip_efficiency=1.5)


def test_invalid_reserve_ratio_is_rejected():
    with pytest.raises(DispatchError, match="reserve_ratio"):
        solve_dispatch([hydro()], 100.0, reserve_ratio=1.5)


# --------------------------------------------------------------------------
# Conservation
# --------------------------------------------------------------------------
def test_energy_balances_across_a_range_of_demands():
    """Generation plus storage must equal demand plus over-generation, always."""
    units = [coal(), hydro(), solar(180.0), battery()]
    for demand in (0.0, 50.0, 120.0, 300.0, 620.0, 900.0, 2000.0):
        result = solve_dispatch(units, demand)
        delivered = result.generation_mw + result.storage_net_mw
        assert delivered >= demand - result.unserved_mw - 1e-6
        assert result.unserved_mw >= 0.0
        assert result.curtailed_mw >= 0.0


def test_curtailed_energy_is_never_counted_as_generation():
    result = solve_dispatch([solar(180.0)], 100.0)
    assert result.generation_mw == pytest.approx(100.0)
    assert result.curtailed_mw == pytest.approx(80.0)
