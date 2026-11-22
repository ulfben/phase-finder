"""Run the analysis core on a recorder SQLite database, read-only. For tuning, not shipped.

Usage on the HA host (the analysis module is prepended so this runs as one stdin script):
    cat custom_components/phase_finder/analysis.py tools/replay_sqlite.py | ssh ha "python3 -"
Edit CONFIGS / LOADS below.
"""
# ruff: noqa: F821, E402  (names come from analysis.py, prepended)
import json
import sqlite3
import time

DB = "file:/config/home-assistant_v2.db?mode=ro"
db = sqlite3.connect(DB, uri=True, timeout=30)

LOADS = """sensor.dishwasher_power sensor.kitchen_island_power sensor.tz3000_j1v25l17_ts011f_active_power
sensor.lumi_lumi_plug_maeu01_active_power_3 sensor.lumi_lumi_plug_maeu01_electricalmeasurement_2
sensor.power_to_ulfs_desk_2_active_power_2 sensor.johans_desk_power
sensor.power_to_laundry_machine_active_power sensor.lumi_lumi_plug_maeu01_active_power_4
sensor.guest_room_2_workshop_lights_vent_power sensor.power_to_guest_kitchen_power
sensor.power_to_bsmnt_fridge_power sensor.lumi_lumi_plug_maeu01_electricalmeasurement
sensor.lumi_lumi_plug_maeu01_active_power sensor.bedroom_radiator_power
sensor.lumi_lumi_plug_maeu01_active_power_2 sensor.llvp_power_measure_power
sensor.ftx_power_measure_power
sensor.shellypro3em_a0dd6c9f8f3c_phase_a_active_power sensor.shellypro3em_a0dd6c9f8f3c_phase_b_active_power
sensor.shellypro3em_a0dd6c9f8f3c_phase_c_active_power sensor.shellypro3em_a0dd6c9f8f3c_total_active_power
sensor.shellypro_spis_ugn_phase_a_active_power sensor.shellypro_spis_ugn_phase_b_active_power
sensor.shellypro_spis_ugn_phase_c_active_power""".split()

P1 = "sensor.electricity_meter_power_{}_phase_l{}"
INV = "sensor.meter_phase_{}_active_power_inv_1"


def unit(eid):
    row = db.execute(
        "SELECT a.shared_attrs FROM states s JOIN states_meta m ON s.metadata_id=m.metadata_id "
        "JOIN state_attributes a ON s.attributes_id=a.attributes_id WHERE m.entity_id=? "
        "ORDER BY s.last_updated_ts DESC LIMIT 1",
        (eid,),
    ).fetchone()
    return json.loads(row[0]).get("unit_of_measurement") if row else None


def scale(eid):
    return {"kW": 1000.0, "MW": 1e6}.get(unit(eid), 1.0)


def load_series(eid):
    r = db.execute("SELECT metadata_id FROM states_meta WHERE entity_id=?", (eid,)).fetchone()
    if not r:
        return Series()
    out = []
    for t, s in db.execute(
        "SELECT last_updated_ts, state FROM states WHERE metadata_id=? ORDER BY last_updated_ts",
        (r[0],),
    ):
        try:
            v = float(s)
        except (TypeError, ValueError):
            v = None
        out.append((t, v))
    return Series(out)


def p1(net):
    phases = {}
    for i, ph in enumerate(PHASES, 1):
        terms = [Term(P1.format("consumption", i), scale(P1.format("consumption", i)))]
        if net:
            e = P1.format("production", i)
            terms.append(Term(e, scale(e), -1.0))
        phases[ph] = terms
    return Meter("p1_net" if net else "p1_import", phases)


def inv(sign):
    return Meter(
        f"inverter{'+' if sign > 0 else '-'}",
        {ph: [Term(INV.format(c), scale(INV.format(c)), sign)] for ph, c in zip(PHASES, "abc")},
    )


def inv_mapped():
    # The inverter's meter is wired with its phases rotated relative to the P1 meter.
    return Meter("inverter", {"L1": [Term(INV.format("b"))], "L2": [Term(INV.format("c"))],
                              "L3": [Term(INV.format("a"))]})


t0 = time.time()
REF, FAST = p1(True), inv(1.0)  # the inverter meter with its own A/B/C labels, unmapped
sources = set(LOADS) | REF.sources() | FAST.sources()
data = {e: load_series(e) for e in sources}
print(f"loaded {sum(len(s) for s in data.values())} samples in {time.time() - t0:.1f}s")
mapping = {"L1": "L3", "L2": "L1", "L3": "L2"}
day = 86400.0
first = min(min(data[l].ts) for l in LOADS if len(data.get(l, ())))
for load in LOADS:
    buckets = []
    a = first // day * day
    while a < time.time():
        r = analyse_load(load, [REF, FAST], data, Params(), a, a + day,
                         mappings={FAST.name: mapping}, order=[FAST.name, REF.name])
        buckets.append(r.votes)
        a += day
    buckets = [b for b in buckets if b]
    i = detect_move(buckets)
    kept = buckets[i:] if i is not None else buckets
    total = {}
    for b in kept:
        total = merge_votes(total, b)
    v = verdict(total)
    print(f"{load.replace('sensor.', '')[:44]:44} days={len(buckets):2} move_at={i} "
          f"-> {v.phase} conf={v.confidence:.2f}")
