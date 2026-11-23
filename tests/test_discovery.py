"""Meter suggestions from entity ids."""

from custom_components.phase_finder.discovery import phase_token, suggest_meters

P1 = "sensor.electricity_meter_power_{}_phase_l{}"
INV = "sensor.meter_phase_{}_active_power_inv_1"


def test_phase_token() -> None:
    assert phase_token("electricity_meter_power_consumption_phase_l2") == (5, "l2")
    assert phase_token("meter_phase_c_active_power_inv_1") == (2, "l3")
    assert phase_token("dishwasher_power") is None


def test_suggests_p1_with_export_first_then_lettered() -> None:
    ids = [P1.format(k, i) for k in ("consumption", "production") for i in (1, 2, 3)]
    ids += [INV.format(c) for c in "abc"]
    ids += ["sensor.export_power_limit_l1", "sensor.dishwasher_power"]
    meters = suggest_meters(ids, {P1.format("consumption", 1): "SlimmeLezer"})
    assert [m["name"] for m in meters] == ["SlimmeLezer", "meter phase active power inv 1"]
    p1 = meters[0]
    assert p1["l2"] == P1.format("consumption", 2)
    assert p1["l2_export"] == P1.format("production", 2)
    assert "l1_export" not in meters[1]


def test_incomplete_meter_is_not_suggested() -> None:
    assert suggest_meters(["sensor.power_l1", "sensor.power_l2"]) == []


def test_current_meters_come_after_power_meters() -> None:
    amps = [f"sensor.current_l{i}_house" for i in (1, 2, 3)]
    watts = [f"sensor.power_l{i}_house" for i in (1, 2, 3)]
    meters = suggest_meters([*amps, *watts], currents=amps)
    assert [m["l1"] for m in meters] == [watts[0], amps[0]]
