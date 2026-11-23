"""The analysis core, on synthetic history."""

from __future__ import annotations

import random

from custom_components.phase_finder import analysis as an

PARAMS = an.Params()


def _house(
    events: list[tuple[float, dict[str, float], float]],
    duration: float = 6000.0,
    period: float = 5.0,
    labels: tuple[str, str, str] = ("L1", "L2", "L3"),
    seed: int = 1,
) -> dict[str, an.Series]:
    """Phase readings every `period` s with noise, plus one plug, from (t, phase deltas, plug)."""
    rng = random.Random(seed)
    base = {"L1": 800.0, "L2": 600.0, "L3": 400.0}
    plug: list[tuple[float, float | None]] = [(0.0, 0.0)]
    level = 0.0
    events = sorted(events, key=lambda e: e[0])
    for t, _deltas, plug_delta in events:
        level += plug_delta
        plug.append((t + 1.0, level))  # the plug reports a second after the change
    phases: dict[str, list[tuple[float, float | None]]] = {p: [] for p in an.PHASES}
    t = 0.0
    while t < duration:
        for phase in an.PHASES:
            value = base[phase] + sum(d.get(phase, 0.0) for at, d, _ in events if at <= t)
            phases[phase].append((t, value + rng.uniform(-5, 5)))
        t += period
    data = {
        f"meter_{label}": an.Series(phases[p]) for p, label in zip(an.PHASES, labels, strict=False)
    }
    data["plug"] = an.Series(plug)
    return data


def _meter(labels: tuple[str, str, str] = ("L1", "L2", "L3"), name: str = "m") -> an.Meter:
    return an.Meter(
        name, {p: [an.Term(f"meter_{lab}")] for p, lab in zip(an.PHASES, labels, strict=False)}
    )


def _on_off(phase_deltas: dict[str, float], plug: float, count: int = 10) -> list:
    events = []
    for i in range(count):
        t = 200.0 + i * 500.0
        events.append((t, phase_deltas, plug))
        events.append((t + 200.0, {k: -v for k, v in phase_deltas.items()}, -plug))
    return events


def test_single_phase_load() -> None:
    data = _house(_on_off({"L2": 500.0}, 500.0))
    result = an.analyse_load("plug", [_meter()], data, PARAMS)
    assert result.steps == 20
    assert result.votes == {"L2": 20}
    v = an.verdict(result.votes)
    assert v.phase == "L2"
    assert an.is_certain(v)


def test_two_phase_heater() -> None:
    data = _house(_on_off({"L1": 1000.0, "L3": 1000.0}, 2000.0))
    result = an.analyse_load("plug", [_meter()], data, PARAMS)
    assert result.votes == {"L1+L3": 20}
    assert an.verdict(result.votes).phase == "L1+L3"


def test_small_and_coincident_steps_do_not_vote() -> None:
    events = _on_off({"L1": 20.0}, 20.0)  # below min_step_w
    # Two loads switching at once on different phases with the same power: ambiguous.
    events += [(5600.0, {"L1": 300.0, "L2": 300.0}, 300.0)]
    data = _house(events, duration=6000.0)
    result = an.analyse_load("plug", [_meter()], data, PARAMS)
    assert result.votes == {}


def test_stale_meter_is_skipped() -> None:
    data = _house(_on_off({"L3": 400.0}, 400.0), period=120.0)
    result = an.analyse_load("plug", [_meter()], data, PARAMS)
    assert result.tested < result.steps


def test_learns_rotated_meter() -> None:
    events = []
    for offset, phase in ((0.0, "L1"), (50.0, "L2"), (100.0, "L3")):
        events += [
            (t + offset, d and {phase: d["X"]}, p) for t, d, p in _on_off({"X": 500.0}, 500.0)
        ]
    data = _house(events)
    # Another meter wired in another order: its L1 is the reference's L2, and so on.
    other = an.Meter(
        "other",
        {"L1": [an.Term("meter_L2")], "L2": [an.Term("meter_L3")], "L3": [an.Term("meter_L1")]},
    )
    meters = [_meter(), other]
    result = an.analyse_load("plug", meters, data, PARAMS)
    mapping = an.learn_mapping(result.cross["other"])
    assert mapping == {"L1": "L2", "L2": "L3", "L3": "L1"}
    # Before the mapping is known the other meter does not vote; after, it votes in the
    # reference's labels.
    assert result.meters_used == {"m": result.tested}
    mapped = an.analyse_load(
        "plug", meters, data, PARAMS, mappings={"other": mapping}, order=["other", "m"]
    )
    assert mapped.meters_used == {"other": mapped.tested}
    assert mapped.votes == result.votes


def test_export_is_subtracted() -> None:
    # Exporting 2 kW on L1; the plug turns on and export drops by its power.
    imp = an.Series([(t, 0.0) for t in range(0, 1000, 5)])
    exp = an.Series([(t, 2000.0 if t < 500 else 1600.0) for t in range(0, 1000, 5)])
    flat = an.Series([(t, 300.0 + (t % 10)) for t in range(0, 1000, 5)])
    data = {
        "i1": imp,
        "e1": exp,
        "l2": flat,
        "l3": flat,
        "plug": an.Series([(0, 0.0), (501, 400.0)]),
    }
    meter = an.Meter(
        "p1",
        {
            "L1": [an.Term("i1"), an.Term("e1", sign=-1.0)],
            "L2": [an.Term("l2")],
            "L3": [an.Term("l3")],
        },
    )
    assert an.analyse_load("plug", [meter], data, PARAMS).votes == {"L1": 1}


# The washing machine's votes per day, from a real house: moved from L2 to L3 in between.
WASHER_DAYS = [
    {"L1": 1, "L1+L3": 1, "L2": 24, "L2+L3": 1, "L3": 8},
    {"L1": 7, "L1+L2": 1, "L1+L2+L3": 3, "L1+L3": 19, "L2": 104, "L2+L3": 7, "L3": 16},
    {"L1": 2, "L1+L2": 5, "L1+L2+L3": 1, "L1+L3": 2, "L2": 7, "L2+L3": 6, "L3": 37},
]


def test_detects_a_moved_load() -> None:
    assert an.detect_move(WASHER_DAYS) == 2
    after = WASHER_DAYS[2]
    assert an.verdict(after).phase == "L3"


def test_no_move_for_a_steady_load() -> None:
    days = [{"L3": 20, "L1": 2}, {"L3": 15, "L1+L3": 3}, {"L3": 30, "L2": 1}]
    assert an.detect_move(days) is None
    assert an.detect_move([{"L1": 4}, {"L2": 5}]) is None  # too few votes to call it a move


def test_verdict_two_phase_loads() -> None:
    assert an.verdict({"L1+L3": 30, "L3": 4}).phase == "L1+L3"  # a 400 V heater
    assert an.verdict({"L2": 50, "L3": 40, "L1": 3}).phase == "L2+L3"  # heater and motor


def test_verdict_pooled_move_leans_single() -> None:
    # Before move detection splits them, pooled votes with a small second phase stay single.
    assert an.verdict({"L2": 136, "L3": 55, "L1": 10, "L1+L3": 22}).phase == "L2"


def test_verdict_ignores_a_few_stray_votes() -> None:
    assert an.verdict({"L2": 32, "L2+L3": 2, "L3": 1}).phase == "L2"
    assert an.verdict({"L3": 44, "L1+L3": 6, "L2": 5, "L1": 2}).phase == "L3"
    assert an.verdict({"L1": 2}).phase is None  # too few votes


def test_publish_keeps_current_until_clearly_beaten() -> None:
    weak = an.verdict({"L1": 6, "L3": 5})
    assert an.publish("L3", weak) == "L3"
    strong = an.verdict({"L1": 40, "L3": 2})
    assert an.publish("L3", strong) == "L1"
    assert an.publish(None, weak) == "L1"


def test_decay() -> None:
    assert an.decay({"L1": 8.0}, 10.0, 10.0) == {"L1": 4.0}
    assert an.decay({"L1": 8.0}, 0.0, 10.0) == {"L1": 8.0}


def test_merged_ramp_is_one_step() -> None:
    series = an.Series([(0, 0.0), (100, 300.0), (102, 900.0), (104, 1500.0), (300, 0.0)])
    steps = an.find_steps(series, PARAMS)
    assert [round(s.delta) for s in steps] == [1500, -1500]
    assert steps[0].t_start == 100 and steps[0].t_end == 104


def test_unavailable_breaks_steps() -> None:
    series = an.Series([(0, 0.0), (10, None), (20, 500.0), (30, 0.0)])
    assert [s.delta for s in an.find_steps(series, PARAMS)] == [-500.0]


def test_current_meter_matches_either_sign() -> None:
    # A current has no sign: on a phase that exports, a load turning on lowers the current.
    deltas = {"L1": 3.0, "L2": -480.0, "L3": 10.0}
    assert an.classify(500.0, deltas, PARAMS, unsigned=True) == "L2"
    assert an.classify(500.0, deltas, PARAMS) is None  # a signed meter would not accept it
    # ...but it can't tell a split over two phases.
    assert an.classify(1000.0, {"L1": 500.0, "L2": 0.0, "L3": 500.0}, PARAMS, True) is None
