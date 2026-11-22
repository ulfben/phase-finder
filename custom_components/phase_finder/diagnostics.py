"""Diagnostics: the options and everything learned so far (no secrets are involved)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PhaseFinderConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PhaseFinderConfigEntry
) -> dict[str, Any]:
    finder = entry.runtime_data
    return {
        "options": dict(entry.options),
        "active_loads": sorted(
            finder.loads[k].entity_id for k in finder.active_keys() if k in finder.loads
        ),
        "state": finder.as_dict(),
    }
