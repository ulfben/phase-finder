"""Config and options flow: pick the phase meters and the loads to analyse."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO,
    CONF_EXCLUDE,
    CONF_HALF_LIFE_DAYS,
    CONF_INCLUDE,
    CONF_INTERVAL_DAYS,
    CONF_INVERT,
    CONF_METERS,
    CONF_MIN_STEP_W,
    CONF_NAME,
    CONF_WINDOW_S,
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_INTERVAL_DAYS,
    DEFAULT_MIN_STEP_W,
    DEFAULT_WINDOW_S,
    DOMAIN,
    EXPORT_FIELDS,
    PHASE_FIELDS,
    TITLE,
)
from .discovery import find_loads, power_sensors, suggest_meters

MANUAL = "manual"
CONF_REFERENCE = "reference"
CONF_EXTRA = "extra"
CONF_ADD_MANUAL = "add_manual"
CONF_ADD_ANOTHER = "add_another"


def _power_selector(multiple: bool = False) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=multiple)
    )


def _number(minimum: float, maximum: float, step: float, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _meter_schema(defaults: dict[str, Any] | None, ask_another: bool) -> vol.Schema:
    d = defaults or {}
    fields: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): selector.TextSelector(),
    }
    for key in PHASE_FIELDS:
        fields[vol.Required(key, description={"suggested_value": d.get(key)})] = _power_selector()
    for key in EXPORT_FIELDS:
        fields[vol.Optional(key, description={"suggested_value": d.get(key)})] = _power_selector()
    fields[vol.Required(CONF_INVERT, default=bool(d.get(CONF_INVERT, False)))] = (
        selector.BooleanSelector()
    )
    if ask_another:
        fields[vol.Required(CONF_ADD_ANOTHER, default=False)] = selector.BooleanSelector()
    return vol.Schema(fields)


def _clean_meter(user_input: dict[str, Any]) -> dict[str, Any]:
    meter = {k: user_input[k] for k in (CONF_NAME, *PHASE_FIELDS)}
    meter[CONF_NAME] = meter[CONF_NAME] or user_input["l1"]
    for key in EXPORT_FIELDS:
        if user_input.get(key):
            meter[key] = user_input[key]
    if user_input.get(CONF_INVERT):
        meter[CONF_INVERT] = True
    return meter


def _meter_errors(meter: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, str]:
    picked = [meter[k] for k in (*PHASE_FIELDS, *EXPORT_FIELDS) if meter.get(k)]
    if len(set(picked)) != len(picked):
        return {"base": "same_sensor_twice"}
    if any(meter["l1"] == other["l1"] for other in others):
        return {"base": "meter_already_added"}
    return {}


def _names(hass: HomeAssistant) -> dict[str, str]:
    """Entity id -> the device name, for labelling suggested meters."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    names: dict[str, str] = {}
    for entity_id in power_sensors(hass):
        entry = ent_reg.async_get(entity_id)
        device = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
        if device:
            names[entity_id] = device.name_by_user or device.name or entity_id
    return names


def _meter_label(hass: HomeAssistant, meter: dict[str, Any]) -> str:
    sensors = [meter[k].partition(".")[2] for k in PHASE_FIELDS]
    parts = [meter.get(CONF_NAME) or sensors[0], " / ".join(sensors)]
    if meter.get("l1_export"):
        parts.append("minus export")
    return " · ".join(parts)


def _loads_schema(options: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_AUTO, default=options.get(CONF_AUTO, True)): (
                selector.BooleanSelector()
            ),
            vol.Optional(
                CONF_INCLUDE, description={"suggested_value": options.get(CONF_INCLUDE, [])}
            ): _power_selector(multiple=True),
            vol.Optional(
                CONF_EXCLUDE, description={"suggested_value": options.get(CONF_EXCLUDE, [])}
            ): _power_selector(multiple=True),
        }
    )


def _loads_placeholders(
    hass: HomeAssistant, options: dict[str, Any], meters: list[dict[str, Any]]
) -> dict[str, str]:
    found = find_loads(hass, {CONF_AUTO: True}, meters)
    return {
        "count": str(len(found)),
        "loads": "\n".join(f"- {e}" for e in found) or "- (none)",
    }


class _MeterSteps:
    """Steps shared by the config flow and the options flow."""

    hass: HomeAssistant
    _meters: list[dict[str, Any]]
    _suggested: list[dict[str, Any]]

    def _suggest(self) -> list[dict[str, Any]]:
        taken = {m["l1"] for m in self._meters}
        return [m for m in self._suggested if m["l1"] not in taken]

    async def async_step_reference(self, user_input: dict[str, Any] | None = None):
        self._suggested = suggest_meters(power_sensors(self.hass), _names(self.hass))
        if user_input is not None:
            choice = user_input[CONF_REFERENCE]
            if choice == MANUAL:
                return await self.async_step_reference_manual()
            self._meters = [self._suggested[int(choice)]]
            return await self.async_step_extra()
        options = [
            selector.SelectOptionDict(value=str(i), label=_meter_label(self.hass, m))
            for i, m in enumerate(self._suggested)
        ]
        options.append(selector.SelectOptionDict(value=MANUAL, label="manual"))
        default = "0" if self._suggested else MANUAL
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="reference",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REFERENCE, default=default): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key=CONF_REFERENCE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_reference_manual(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            meter = _clean_meter(user_input)
            errors = _meter_errors(meter, [])
            if not errors:
                self._meters = [meter]
                return await self.async_step_extra()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="reference_manual",
            data_schema=_meter_schema(user_input, ask_another=False),
            errors=errors,
        )

    async def async_step_extra(self, user_input: dict[str, Any] | None = None):
        candidates = self._suggest()
        if user_input is not None:
            for value in user_input.get(CONF_EXTRA) or []:
                self._meters.append(candidates[int(value)])
            if user_input.get(CONF_ADD_MANUAL):
                return await self.async_step_extra_manual()
            return await self.async_step_loads()
        fields: dict[Any, Any] = {}
        if candidates:
            fields[vol.Optional(CONF_EXTRA, default=[])] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=str(i), label=_meter_label(self.hass, m))
                        for i, m in enumerate(candidates)
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        fields[vol.Required(CONF_ADD_MANUAL, default=False)] = selector.BooleanSelector()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="extra",
            data_schema=vol.Schema(fields),
            description_placeholders={"reference": _meter_label(self.hass, self._meters[0])},
        )

    async def async_step_extra_manual(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            meter = _clean_meter(user_input)
            errors = _meter_errors(meter, self._meters)
            if not errors:
                self._meters.append(meter)
                if user_input.get(CONF_ADD_ANOTHER):
                    return await self.async_step_extra_manual()
                return await self.async_step_loads()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="extra_manual",
            data_schema=_meter_schema(user_input, ask_another=True),
            errors=errors,
        )

    async def async_step_loads(self, user_input: dict[str, Any] | None = None):
        raise NotImplementedError


class PhaseFinderConfigFlow(_MeterSteps, ConfigFlow, domain=DOMAIN):
    """Set up Phase Finder."""

    VERSION = 1

    def __init__(self) -> None:
        self._meters = []
        self._suggested = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_reference()

    async def async_step_loads(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = {
                CONF_METERS: self._meters,
                CONF_AUTO: user_input[CONF_AUTO],
                CONF_INCLUDE: user_input.get(CONF_INCLUDE, []),
                CONF_EXCLUDE: user_input.get(CONF_EXCLUDE, []),
            }
            return self.async_create_entry(title=TITLE, data={}, options=options)
        return self.async_show_form(
            step_id="loads",
            data_schema=_loads_schema({}),
            description_placeholders=_loads_placeholders(self.hass, {}, self._meters),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PhaseFinderOptionsFlow()


class PhaseFinderOptionsFlow(_MeterSteps, OptionsFlow):
    """Change the meters, the loads or the settings."""

    def __init__(self) -> None:
        self._meters = []
        self._suggested = []
        self._meters_changed = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["reference", "loads", "settings"])

    async def async_step_loads(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        meters = self._meters or list(options.get(CONF_METERS) or [])
        if user_input is not None:
            options.update(
                {
                    CONF_METERS: meters,
                    CONF_AUTO: user_input[CONF_AUTO],
                    CONF_INCLUDE: user_input.get(CONF_INCLUDE, []),
                    CONF_EXCLUDE: user_input.get(CONF_EXCLUDE, []),
                }
            )
            return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="loads",
            data_schema=_loads_schema(options),
            description_placeholders=_loads_placeholders(self.hass, options, meters),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        if user_input is not None:
            options.update({k: int(v) for k, v in user_input.items()})
            return self.async_create_entry(data=options)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INTERVAL_DAYS,
                    default=options.get(CONF_INTERVAL_DAYS, DEFAULT_INTERVAL_DAYS),
                ): _number(1, 30, 1, "d"),
                vol.Required(
                    CONF_HALF_LIFE_DAYS,
                    default=options.get(CONF_HALF_LIFE_DAYS, DEFAULT_HALF_LIFE_DAYS),
                ): _number(7, 365, 1, "d"),
                vol.Required(
                    CONF_MIN_STEP_W, default=options.get(CONF_MIN_STEP_W, DEFAULT_MIN_STEP_W)
                ): _number(10, 1000, 5, "W"),
                vol.Required(
                    CONF_WINDOW_S, default=options.get(CONF_WINDOW_S, DEFAULT_WINDOW_S)
                ): _number(4, 60, 1, "s"),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
