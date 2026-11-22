"""Phase Finder: learn which mains phase each load is on from recorded power history."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import ATTR_FULL, DOMAIN, SERVICE_FORGET, SERVICE_RESCAN, STORAGE_VERSION
from .finder import PhaseFinder

type PhaseFinderConfigEntry = ConfigEntry[PhaseFinder]

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
RESCAN_SCHEMA = vol.Schema({vol.Optional(ATTR_FULL, default=False): cv.boolean})
FORGET_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the rescan action."""

    async def _rescan(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_run(full=call.data[ATTR_FULL])

    async def _forget(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_forget(call.data[ATTR_ENTITY_ID])

    hass.services.async_register(DOMAIN, SERVICE_RESCAN, _rescan, schema=RESCAN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_FORGET, _forget, schema=FORGET_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PhaseFinderConfigEntry) -> bool:
    finder = PhaseFinder(hass, entry)
    await finder.async_load()
    entry.runtime_data = finder
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await finder.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PhaseFinderConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the saved votes."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_reload_entry(hass: HomeAssistant, entry: PhaseFinderConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
