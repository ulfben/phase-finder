# <img alt="" src="https://raw.githubusercontent.com/ulfben/phase-finder/main/custom_components/phase_finder/brand/icon@2x.png" height="48" align="top"> Phase Finder

[![Validate](https://github.com/ulfben/phase-finder/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/ulfben/phase-finder/actions/workflows/validate.yml)

A Home Assistant integration that works out which mains phase (L1, L2 or L3) each of your
smart plugs and circuit meters is on, from the power history Home Assistant already records.

Knowing the phase helps you spread big loads across the main fuse, plan where to plug in the
heater, and read your per-phase meter: "L3 is high because the dishwasher and the desk are on it".
Nobody needs to open the fuse box.

## How it works

When a plug's power jumps by, say, +2000 W, the phase it is wired to jumps by about 2000 W
at the same moment and the other two phases don't. Phase Finder looks for those steps in the
recorder history and compares them with a meter that reports power per phase (a P1/DSMR
reader, an inverter's grid meter, a Tibber Pulse, a clamp meter...).

- **Each clear step is one vote.** Steps that coincide with another load switching on another
  phase are ambiguous and are skipped, so a few coincidences can't decide the answer.
- **Multi-phase loads** are recognised: a 400 V heater across two phases, or a washing machine
  with its heater on one phase and its motor on another, is reported as `L2+L3`.
- **Votes add up across runs** and fade slowly (60-day half-life by default), so the answer gets
  surer over time, even beyond the recorder's retention, and follows a plug you move.
- **Answers don't flip-flop:** once shown, a phase changes only when a new answer is clearly
  stronger.
- **Several meters:** a meter that reports every few seconds finds far more phases than one that
  reports every minute. Add the fast one as an extra meter. Phase Finder learns how its phase
  labels match the reference meter's (many inverters use another order than the grid company),
  and uses it only once that mapping is clear.
- **Works with any recorder database** (SQLite, MariaDB, PostgreSQL) through the recorder's
  history API. Each day of history is read and analysed separately, in the recorder's own
  thread, and nothing is written to the database.

Loads that never switch (a network rack, a camera) have no steps, so their phase stays unknown.

## Requirements

- Home Assistant 2025.1 or newer, with the recorder enabled (it is by default).
- A 3-phase connection and a meter that reports **power per phase** on the main fuse.
- Power sensors (W or kW) on the loads you want to place: smart plugs, Shelly relays with power
  metering, sub-meter channels...

## Installation

**HACS**: add `https://github.com/ulfben/phase-finder` as a custom repository (type
*Integration*), install **Phase Finder** and restart Home Assistant.

**Manually**: copy `custom_components/phase_finder` into your `config/custom_components/` folder
and restart Home Assistant.

Then go to **Settings → Devices & services → Add integration → Phase Finder**.

## Setup

1. **Reference meter.** Phase Finder suggests the meters it finds (sensors whose names differ only
   in the phase, such as `..._phase_l1/_l2/_l3`, with the export side subtracted when there is
   one). Pick the one on your main fuse whose L1/L2/L3 you trust, normally the utility meter's
   P1 port. You can also pick the sensors yourself.
2. **More meters** (optional). Add other meters on the main fuse that report more often, such
   as an inverter's grid meter. Not sub-meters that measure only part of the house.
3. **Loads.** By default Phase Finder analyses every power sensor that looks like a single load
   (it skips meters, inverters, template and statistics sensors, totals and forecasts), including
   ones added later. You can add and exclude sensors.

The first analysis runs two minutes after setup, over all the history the recorder keeps. After
that it runs weekly at 03:17 (the interval is in the options).

## Entities

For every analysed load, a sensor named **Phase** on the load's own device, with the entity id
of the power sensor plus `_phase` (`sensor.dishwasher_power` → `sensor.dishwasher_power_phase`):

| | |
|---|---|
| State | `L1`, `L2`, `L3`, `L1+L2`, `L1+L3`, `L2+L3`, `L1+L2+L3`, or unknown |
| `label` | The state for display: `L3`, `L3?` while it is still a guess, `?` when unknown |
| `certain` | `true` once the confidence is at least 0.8 over 8 or more votes |
| `confidence` | 0–1. Two votes out of two gives 0.6; twenty out of twenty gives 0.91 |
| `votes` | The (fading) vote count per phase |
| `candidate` | Present when the current evidence points elsewhere than the state, but not clearly enough to change it yet |

On the **Phase Finder** service device:

- **Last analysis** (diagnostic): when the history was last analysed, with the number of steps
  and votes, and per meter its sample count and the phase mapping learned for it.
- **Analyse now**: analyses the history recorded since the last run.

## Actions and events

- `phase_finder.rescan`: the same as the button. With `full: true` it forgets all votes and
  learned mappings and analyses all the recorded history again.
- Event `phase_finder_phase_changed` (`entity_id`, `old_phase`, `new_phase`) when a load's shown
  phase changes, for example to send a notification.

## Dashboard example

The phase with its guess marker, in an entities card:

```yaml
type: entities
entities:
  - entity: sensor.dishwasher_power_phase
    type: attribute
    attribute: label
    name: Dishwasher phase
```

In a `custom:button-card` template, the phase entity is the power entity plus `_phase`:

```js
[[[ const p = states[`${entity.entity_id}_phase`]; return p ? p.attributes.label : '?'; ]]]
```

## Tuning

**Options → Settings**: analysis interval, vote half-life, the smallest step to learn from
(40 W) and the time window compared around a step (±12 s). A shorter window needs meters that
report more often.

`tools/replay_sqlite.py` runs the analysis on a copy of (or read-only on) a recorder SQLite
database outside Home Assistant, for experimenting with the parameters.

## Limitations

- A P1/DSMR reader that Home Assistant samples only every 30–60 s finds few phases: most steps
  have no fresh reading around them. An inverter or clamp meter that reports every few seconds
  helps a lot. So does a shorter update interval for the reader, if it has one.
- A load whose power only drifts (a thermostat-controlled heater with a dimmer) has no steps.
- Two loads that switch at the same instant on different phases cancel each other's vote.

## License

MIT
