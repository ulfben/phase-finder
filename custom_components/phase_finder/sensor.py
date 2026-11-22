"""A phase sensor per load, and the last analysis run."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import PhaseFinderConfigEntry
from . import analysis as an
from .const import DOMAIN, SIGNAL_UPDATED, TITLE
from .finder import LoadState, PhaseFinder

_POWER_SUFFIX = re.compile(r"\s*(active\s+)?power(\s+measure(ment)?)?$", re.IGNORECASE)


def service_device(finder: PhaseFinder) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, finder.entry.entry_id)},
        name=TITLE,
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PhaseFinderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    finder = entry.runtime_data
    keys = finder.sensor_keys()
    loads = [finder.loads[k] for k in sorted(keys)]
    _remove_stale(hass, entry.entry_id, keys)
    finder.mark_known(keys)
    async_add_entities([LastRunSensor(finder), *(_phase_sensors(hass, finder, loads))])

    @callback
    def _new(loads: list[LoadState]) -> None:
        async_add_entities(_phase_sensors(hass, finder, loads))

    entry.async_on_unload(finder.async_add_new_load_listener(_new))


def _remove_stale(hass: HomeAssistant, entry_id: str, keys: set[str]) -> None:
    """Remove phase sensors of excluded or deleted loads, and stale device links."""
    ent_reg = er.async_get(hass)
    wanted = {f"{entry_id}_{key}" for key in keys} | {f"{entry_id}_last_run"}
    for reg in er.async_entries_for_config_entry(ent_reg, entry_id):
        if reg.domain == "sensor" and reg.unique_id not in wanted:
            ent_reg.async_remove(reg.entity_id)
    # Older versions linked the phase sensors through DeviceInfo, which added this entry
    # to the loads' devices. Only the service device should belong to it.
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
        if (DOMAIN, entry_id) not in device.identifiers:
            dev_reg.async_update_device(device.id, remove_config_entry_id=entry_id)


def _phase_sensors(
    hass: HomeAssistant, finder: PhaseFinder, loads: list[LoadState]
) -> list[PhaseSensor]:
    ent_reg = er.async_get(hass)
    # How many analysed loads each device has: one gets the name "Phase", several get
    # "<sensor name> phase" so they can be told apart.
    per_device: dict[str, int] = {}
    for key in finder.sensor_keys():
        load = finder.loads.get(key)
        entry = ent_reg.async_get(load.entity_id) if load else None
        if entry and entry.device_id:
            per_device[entry.device_id] = per_device.get(entry.device_id, 0) + 1
    return [PhaseSensor(hass, finder, load, per_device) for load in loads]


class PhaseSensor(SensorEntity):
    """The phase a load is on: L1, L2, L3, or several for a multi-phase load."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(an.ALL_KEYS)
    _attr_icon = "mdi:sine-wave"

    def __init__(
        self,
        hass: HomeAssistant,
        finder: PhaseFinder,
        load: LoadState,
        per_device: dict[str, int],
    ) -> None:
        self.finder = finder
        self.load_key = load.key
        self._attr_unique_id = f"{finder.entry.entry_id}_{load.key}"
        source = load.entity_id
        entry = er.async_get(hass).async_get(source)
        device = (
            dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
        )
        # The source's own name without "power": "Output 0 power" -> "Output 0".
        own = _POWER_SUFFIX.sub("", (entry.name or entry.original_name or "") if entry else "")
        if device is not None:
            # Shown on the load's own device, like a helper. Setting device_entry links the
            # entity without adding this integration to the other integration's device.
            self.device_entry = device
            shared = per_device.get(device.id, 0) > 1
            self._attr_name = f"{own.strip()} phase" if shared and own.strip() else "Phase"
        else:
            self._attr_device_info = service_device(finder)
            state = hass.states.get(source)
            base = own.strip() or _POWER_SUFFIX.sub("", state.name if state else source).strip()
            self._attr_name = f"{base or source} phase"
        # sensor.dishwasher_power -> sensor.dishwasher_power_phase, so dashboards can find it.
        self.entity_id = f"sensor.{source.partition('.')[2]}_phase"

    @property
    def _load(self) -> LoadState | None:
        return self.finder.loads.get(self.load_key)

    @property
    def native_value(self) -> str | None:
        load = self._load
        return load.published if load else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        load = self._load
        if load is None:
            return {}
        v = load.verdict
        certain = an.is_certain(v) and v.phase == load.published
        label = load.published or "?"
        if load.published and not certain:
            label += "?"
        attrs: dict[str, Any] = {
            "source_entity_id": load.entity_id,
            "label": label,
            "certain": certain,
            "confidence": round(v.confidence, 2),
            "votes": {k: round(n, 1) for k, n in sorted(load.votes.items())},
            "steps_tested": int(load.tested),
            "last_vote": dt_util.utc_from_timestamp(load.last_vote_ts).isoformat()
            if load.last_vote_ts
            else None,
        }
        if v.phase != load.published:
            attrs["candidate"] = v.phase
        return attrs

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATED}_{self.finder.entry.entry_id}",
                self.async_write_ha_state,
            )
        )


class LastRunSensor(SensorEntity):
    """When the history was last analysed, with what it found."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_run"

    def __init__(self, finder: PhaseFinder) -> None:
        self.finder = finder
        self._attr_unique_id = f"{finder.entry.entry_id}_last_run"
        self._attr_device_info = service_device(finder)

    @property
    def native_value(self) -> datetime | None:
        run = self.finder.last_run
        return dt_util.utc_from_timestamp(run["finished_ts"]) if run else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        run = self.finder.last_run or {}
        attrs: dict[str, Any] = {"running": self.finder.running}
        for key in ("duration_s", "loads", "steps", "tested", "votes"):
            if key in run:
                attrs[key] = run[key]
        for key in ("from_ts", "to_ts"):
            if run.get(key):
                attrs[key.removesuffix("_ts")] = dt_util.utc_from_timestamp(run[key]).isoformat()
        if "meters" in run:
            attrs["meters"] = list(run["meters"].values())
        return attrs

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATED}_{self.finder.entry.entry_id}",
                self.async_write_ha_state,
            )
        )
