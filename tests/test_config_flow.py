"""The config and options flows."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.phase_finder.const import DOMAIN

P1 = "sensor.electricity_meter_power_{}_phase_l{}"
POWER = {"device_class": "power", "unit_of_measurement": "kW", "state_class": "measurement"}


def _p1(hass: HomeAssistant) -> None:
    for kind in ("consumption", "production"):
        for i in (1, 2, 3):
            hass.states.async_set(P1.format(kind, i), "0.5", POWER)


@pytest.fixture(autouse=True)
def _no_setup():
    with patch("custom_components.phase_finder.async_setup_entry", return_value=True):
        yield


async def test_flow_with_a_suggested_meter(hass: HomeAssistant) -> None:
    _p1(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reference"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"reference": "0"})
    assert result["step_id"] == "extra"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_manual": False}
    )
    assert result["step_id"] == "loads"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"auto": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    meter = result["options"]["meters"][0]
    assert meter["l1"] == P1.format("consumption", 1)
    assert meter["l3_export"] == P1.format("production", 3)
    assert result["options"]["auto"] is True


async def test_flow_with_manual_meters(hass: HomeAssistant) -> None:
    _p1(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"reference": "manual"}
    )
    assert result["step_id"] == "reference_manual"
    same = {"name": "P1", "l1": P1.format("consumption", 1), "invert": False}
    same |= {"l2": same["l1"], "l3": P1.format("consumption", 3)}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], same)
    assert result["errors"] == {"base": "same_sensor_twice"}
    good = same | {"l2": P1.format("consumption", 2)}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], good)
    assert result["step_id"] == "extra"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"add_manual": True})
    assert result["step_id"] == "extra_manual"
    other = {
        "name": "Inverter",
        "l1": P1.format("production", 1),
        "l2": P1.format("production", 2),
        "l3": P1.format("production", 3),
        "invert": True,
        "add_another": False,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], other)
    assert result["step_id"] == "loads"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auto": False, "include": ["sensor.plug_power"]}
    )
    meters = result["options"]["meters"]
    assert [m["name"] for m in meters] == ["P1", "Inverter"]
    assert meters[1]["invert"] is True
    assert "invert" not in meters[0]


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, options={"meters": []}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_options_settings(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, options={"meters": [], "auto": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"interval_days": 3, "half_life_days": 30, "min_step_w": 50, "window_s": 10},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["interval_days"] == 3
    assert entry.options["auto"] is True
