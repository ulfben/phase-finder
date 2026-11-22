"""A button that analyses the history recorded since the last run."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PhaseFinderConfigEntry
from .const import DOMAIN
from .finder import PhaseFinder
from .sensor import service_device


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PhaseFinderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([AnalyseButton(entry.runtime_data)])


class AnalyseButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "analyse"

    def __init__(self, finder: PhaseFinder) -> None:
        self.finder = finder
        self._attr_unique_id = f"{finder.entry.entry_id}_analyse"
        self._attr_device_info = service_device(finder)

    async def async_press(self) -> None:
        self.finder.entry.async_create_background_task(
            self.hass, self.finder.async_run(), f"{DOMAIN} analyse"
        )
