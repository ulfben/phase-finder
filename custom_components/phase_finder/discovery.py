"""Find phase meters and loads among the power sensors."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_AUTO,
    CONF_EXCLUDE,
    CONF_INCLUDE,
    CONF_NAME,
    CURRENT_UNITS,
    DERIVED_PLATFORMS,
    EXPORT_FIELDS,
    MAX_POWER_SENSORS_PER_DEVICE,
    PHASE_FIELDS,
    POWER_UNITS,
)

_NUMBERED = {"1": "l1", "2": "l2", "3": "l3", "l1": "l1", "l2": "l2", "l3": "l3"}
_LETTERED = {"a": "l1", "b": "l2", "c": "l3"}
# Words that mark a sensor as the export (production, returned) side of a meter.
_EXPORT_WORDS = frozenset({"production", "export", "returned", "feed", "injection", "produced"})
_IMPORT_WORDS = frozenset({"consumption", "import", "consumed", "usage"})
# Words that mark a value derived from power, not power itself.
_NOT_POWER = frozenset({"limit", "max", "min", "average", "avg", "rated", "forecast", "peak"})


def phase_token(object_id: str) -> tuple[int, str] | None:
    """(token index, 'l1'..'l3') for the phase part of an object id, else None."""
    tokens = object_id.split("_")
    for i, token in enumerate(tokens):
        if token in ("l1", "l2", "l3"):
            return i, token
        if token == "phase" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if nxt in _NUMBERED:
                return i + 1, _NUMBERED[nxt]
            if nxt in _LETTERED:
                return i + 1, _LETTERED[nxt]
    return None


@dataclass
class _Group:
    key: str  # object id with the phase token replaced by '*'
    words: frozenset[str]
    sensors: dict[str, str]


def suggest_meters(
    entity_ids: Iterable[str],
    names: Mapping[str, str] | None = None,
    currents: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Meters whose sensors differ only in the phase, with an export side when there is one.

    Returns meter option dicts ({name, l1, l2, l3[, l1_export, ...]}): power meters before
    current meters (those in `currents`), and numbered (L1/L2/L3) before lettered (A/B/C)
    ones, since lettered ones often use another order than the house.
    """
    groups: dict[str, _Group] = {}
    for entity_id in entity_ids:
        object_id = entity_id.partition(".")[2]
        found = phase_token(object_id)
        tokens = object_id.split("_")
        if found is None or _NOT_POWER & set(tokens):
            continue
        index, phase = found
        key = "_".join([*tokens[:index], "*", *tokens[index + 1 :]])
        group = groups.setdefault(key, _Group(key, frozenset(tokens), {}))
        group.sensors.setdefault(phase, entity_id)
    complete = {k: g for k, g in groups.items() if len(g.sensors) == 3}

    def base(group: _Group) -> tuple[str, ...]:
        return tuple(t for t in group.key.split("_") if t not in _EXPORT_WORDS | _IMPORT_WORDS)

    exports = {base(g): g for g in complete.values() if g.words & _EXPORT_WORDS}
    current_ids = set(currents)
    meters: list[tuple[bool, bool, str, dict[str, Any]]] = []
    for group in complete.values():
        if group.words & _EXPORT_WORDS:
            continue
        meter: dict[str, Any] = dict(
            zip(PHASE_FIELDS, (group.sensors[p] for p in PHASE_FIELDS), strict=False)
        )
        export = exports.get(base(group))
        if export is not None:
            meter.update(
                zip(EXPORT_FIELDS, (export.sensors[p] for p in PHASE_FIELDS), strict=False)
            )
        first = meter["l1"]
        label = (names or {}).get(first) or group.key.replace("_*", "").replace("_", " ")
        meter[CONF_NAME] = label
        lettered = "_phase_a" in first or "_phase_b" in first
        meters.append((first in current_ids, lettered, label.lower(), meter))
    return [m[3] for m in sorted(meters, key=lambda m: m[:3])]


def _registry_power(entry: er.RegistryEntry) -> bool:
    return (
        entry.domain == "sensor"
        and (entry.device_class or entry.original_device_class) == "power"
        and entry.unit_of_measurement in POWER_UNITS
    )


def current_sensors(hass: HomeAssistant) -> list[str]:
    """Every sensor that reports a current in A or mA (for meters without power per phase)."""
    found = {
        state.entity_id
        for state in hass.states.async_all("sensor")
        if state.attributes.get("device_class") == "current"
        and state.attributes.get("unit_of_measurement") in CURRENT_UNITS
    }
    found |= {
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.domain == "sensor"
        and (e.device_class or e.original_device_class) == "current"
        and e.unit_of_measurement in CURRENT_UNITS
    }
    return sorted(found)


def power_sensors(hass: HomeAssistant) -> list[str]:
    """Every sensor that reports power in W, kW or MW.

    The entity registry is read as well as the states, so a sensor whose integration has not
    reported yet (just after a restart) still counts.
    """
    found = {
        state.entity_id
        for state in hass.states.async_all("sensor")
        if state.attributes.get("device_class") == "power"
        and state.attributes.get("unit_of_measurement") in POWER_UNITS
    }
    found |= {e.entity_id for e in er.async_get(hass).entities.values() if _registry_power(e)}
    return sorted(found)


def meter_entities(meters: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        meter[field]
        for meter in meters
        for field in (*PHASE_FIELDS, *EXPORT_FIELDS)
        if meter.get(field)
    }


def find_loads(
    hass: HomeAssistant, options: Mapping[str, Any], meters: Iterable[Mapping[str, Any]]
) -> list[str]:
    """The power sensors to analyse: discovered ones (if auto) plus included, minus excluded."""
    meters = list(meters)
    taken = meter_entities(meters)
    exclude = set(options.get(CONF_EXCLUDE) or ())
    chosen = set(options.get(CONF_INCLUDE) or ())
    if options.get(CONF_AUTO, True):
        chosen |= set(discover_loads(hass, taken))
    return sorted(e for e in chosen if e not in exclude and e not in taken)


def discover_loads(hass: HomeAssistant, meter_sensors: set[str]) -> list[str]:
    """Power sensors that look like a single load measured at a socket or circuit."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    candidates: list[tuple[str, str]] = []  # (entity_id, device_id)
    per_device: Counter[str] = Counter()
    meter_devices = {
        entry.device_id
        for e in meter_sensors
        if (entry := ent_reg.async_get(e)) is not None and entry.device_id
    }
    for entity_id in power_sensors(hass):
        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.device_id is None:
            continue
        per_device[entry.device_id] += 1
        if entry.disabled_by or entry.hidden_by or entry.platform in DERIVED_PLATFORMS:
            continue
        state = hass.states.get(entity_id)
        state_class = (entry.capabilities or {}).get("state_class") or (
            state.attributes.get("state_class") if state else None
        )
        if state_class != "measurement":
            continue
        tokens = set(entity_id.partition(".")[2].split("_"))
        if "total" in tokens or _NOT_POWER & tokens or _EXPORT_WORDS & tokens:
            continue
        if entity_id in meter_sensors or entry.device_id in meter_devices:
            continue
        device = dev_reg.async_get(entry.device_id)
        if device is None or device.entry_type is not None:  # a service, not hardware
            continue
        candidates.append((entity_id, entry.device_id))
    return [e for e, d in candidates if per_device[d] <= MAX_POWER_SENSORS_PER_DEVICE]
