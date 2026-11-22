"""Constants for Phase Finder."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "phase_finder"
TITLE: Final = "Phase Finder"

# Options: meters
CONF_METERS: Final = "meters"  # list of meter dicts, the first is the reference
CONF_NAME: Final = "name"
CONF_INVERT: Final = "invert"  # the meter reports import as negative
PHASE_FIELDS: Final = ("l1", "l2", "l3")  # import (or net) power per phase
EXPORT_FIELDS: Final = ("l1_export", "l2_export", "l3_export")  # subtracted when set

# Options: loads
CONF_AUTO: Final = "auto"  # analyse every power sensor that looks like a load
CONF_INCLUDE: Final = "include"
CONF_EXCLUDE: Final = "exclude"

# Options: settings
CONF_INTERVAL_DAYS: Final = "interval_days"
CONF_HALF_LIFE_DAYS: Final = "half_life_days"
CONF_MIN_STEP_W: Final = "min_step_w"
CONF_WINDOW_S: Final = "window_s"

DEFAULT_INTERVAL_DAYS: Final = 7
DEFAULT_HALF_LIFE_DAYS: Final = 60
DEFAULT_MIN_STEP_W: Final = 40
DEFAULT_WINDOW_S: Final = 12

# When the scheduled analysis runs (local time).
RUN_HOUR: Final = 3
RUN_MINUTE: Final = 17
# Wait this long after Home Assistant starts before the first analysis.
FIRST_RUN_DELAY_S: Final = 120
# History is read and analysed one chunk at a time to bound memory.
CHUNK_S: Final = 86400.0
# Extra history read before each chunk so the first step has a "before" reading.
PRE_S: Final = 300.0

SERVICE_RESCAN: Final = "rescan"
SERVICE_FORGET: Final = "forget"
ATTR_FULL: Final = "full"
EVENT_PHASE_CHANGED: Final = f"{DOMAIN}_phase_changed"

STORAGE_VERSION: Final = 1
SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"
SIGNAL_NEW_LOADS: Final = f"{DOMAIN}_new_loads"

# Platforms whose power sensors are derived from other sensors, estimated or forecast,
# not measured at a socket. They are left out of automatic discovery.
DERIVED_PLATFORMS: Final = frozenset(
    {
        DOMAIN,
        "derivative",
        "energy",
        "filter",
        "forecast_solar",
        "group",
        "integration",
        "mobile_app",  # a phone's battery, not a mains load
        "min_max",
        "powercalc",
        "solcast_solar",
        "statistics",
        "template",
        "utility_meter",
    }
)
# A device with more power sensors than this is a meter or an inverter, not a load.
MAX_POWER_SENSORS_PER_DEVICE: Final = 4
POWER_UNITS: Final = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}
