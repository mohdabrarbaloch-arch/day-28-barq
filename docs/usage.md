# Usage

## The console

Sign in, and the console loads the grid summary, the fleet and any open alerts.

### KPI row

| KPI | What it means |
|---|---|
| **Total load** | The most recent telemetry reading for the feeder, in MW |
| **Capacity** | Sum of nameplate across every unit on the feeder |
| **Renewable share** | Renewable output as a fraction of total load |
| **Frequency** | Latest grid frequency. 50 Hz nominal; anything outside 49.5–50.5 is a problem |
| **Open alerts** | Unacknowledged alerts, with the critical count underneath |

### Running a dispatch

1. Pick a feeder.
2. Enter the demand for the interval, in MW.
3. Pick the interval length — 15, 30 or 60 minutes. This matters: a 15-minute
   interval gives every unit a quarter of the movement a 60-minute one does.
4. **Solve & commit** persists the plan. **Preview only** solves without writing
   anything, which is what you want while you are exploring.

### Reading the result

The status badge is one of three:

- **balanced** — generation plus storage covers demand exactly
- **surplus** — the fleet is producing more than demand and cannot come down
  fast enough. The excess is real; it is either exported, stored, or curtailed
- **deficit** — demand exceeds what the fleet can deliver. `unserved_mw` is the
  shortfall, and that is load shedding

Below the badge, six figures: generation, storage (positive = discharging),
curtailed, unserved, reserve against requirement, and the marginal price — the
cost of the most expensive unit running, which is what the next MWh would cost.

Then the unit list, sorted by output. Each row shows the setpoint, a bar against
nameplate, and the reason. A `ramp-limited` tag means the unit wanted to go
somewhere it could not reach in one interval.

### Worked example

The demo feeder at 620 MW with 310 MW of renewable potential:

```
status      surplus
generation  520.0 MW
storage     100.0 MW   (discharging)
curtailed   110.0 MW
unserved      0.0 MW
reserve      30.0 / 400.0 MW
```

Read it in order:

1. Solar and wind deliver 310 MW — all of it, because it is free.
2. That leaves 310 MW of demand. The battery discharges its full 100 MW.
3. 210 MW left. Hydro at PKR 3,200 is next, but it is at 0 MW and can only ramp
   to 45 MW/min × 30 min = 1,350 MW, so it takes all 210.
4. Demand is met. Coal and gas stay off.
5. Reserve is 30 MW — hydro's headroom above 210 is 0, and the battery does not
   count as spinning reserve. Coal and gas are not running, so they contribute
   nothing. Against a 400 MW requirement that is a 370 MW shortfall, and the
   alert fires.

That last point is the one worth internalising: **a unit that is off is not
reserve.** It is capacity, and capacity takes hours to become reserve.

## Roles

| Role | Can do |
|---|---|
| `viewer` | Read everything |
| `operator` | Read, ingest telemetry, run dispatch |
| `admin` | Everything, plus manage feeders, assets and user roles |

The first account on a fresh database is the admin. Everyone after that is a
viewer until an admin promotes them.

## Common tasks

**Add a unit to a feeder** — `POST /api/assets` as an admin. The physical limits
matter more than the nameplate: `min_stable_mw` and `ramp_mw_per_min` are what
make the plan realistic.

**Ingest a reading** — `POST /api/telemetry` with `asset_id`, `output_mw` and
`frequency_hz`. Frequency is validated to 40–60 Hz; anything outside that is a
sensor fault, not a grid event.

**Check why a setpoint is what it is** — read the `reason` field. It names the
constraint. If you disagree, you are disagreeing with a specific assumption
rather than with a black box.

**Acknowledge an alert** — `POST /api/alerts/{id}/acknowledge` as an admin. The
alert stays in the table with `acknowledged_at` set; it is not deleted.
