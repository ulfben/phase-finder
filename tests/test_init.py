"""End to end: record history in a real recorder, analyse it, read the phase sensor."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.phase_finder.const import DOMAIN

W = {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"}
METER = {"name": "Meter", "l1": "sensor.grid_l1", "l2": "sensor.grid_l2", "l3": "sensor.grid_l3"}


async def _record_history(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Every 5 s a meter reading; the plug on L2 switches 600 W on and off."""
    freezer.move_to(dt_util.utcnow() - timedelta(hours=3))
    plug_on = False
    for tick in range(1200):
        if tick % 60 == 30:
            plug_on = not plug_on
            hass.states.async_set("sensor.dishwasher_power", "600" if plug_on else "0", W)
        jitter = tick % 7
        base = {"l1": 900, "l2": 500 + (600 if plug_on else 0), "l3": 300}
        for phase, value in base.items():
            hass.states.async_set(f"sensor.grid_{phase}", str(value + jitter), W)
        freezer.tick(timedelta(seconds=5))
        if tick % 100 == 0:
            await hass.async_block_till_done()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=10))


async def test_finds_the_phase(
    recorder_mock, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("test", "plug")}, name="Dishwasher"
    )
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "plug", suggested_object_id="dishwasher_power", device_id=device.id
    )
    hass.states.async_set("sensor.dishwasher_power", "0", W)
    await _record_history(hass, freezer)

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={"meters": [METER], "auto": False, "include": ["sensor.dishwasher_power"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.async_run()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dishwasher_power_phase")
    assert state is not None
    assert state.state == "L2"
    assert state.attributes["label"] == "L2"
    assert state.attributes["certain"] is True
    assert state.attributes["source_entity_id"] == "sensor.dishwasher_power"
    phase_entry = er.async_get(hass).async_get("sensor.dishwasher_power_phase")
    assert phase_entry.device_id == device.id  # shown on the plug's own device

    last = hass.states.get("sensor.phase_finder_last_analysis")
    assert last is not None and last.attributes["votes"] >= 20

    # A second run adds nothing new but keeps the answer; a restart keeps the votes.
    await entry.runtime_data.async_run()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dishwasher_power_phase").state == "L2"

    await hass.services.async_call(DOMAIN, "rescan", {"full": True}, blocking=True)
    assert hass.states.get("sensor.dishwasher_power_phase").state == "L2"
