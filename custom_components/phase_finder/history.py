"""Read power history from the recorder. Runs in the recorder's executor."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .analysis import Series


def _number(state: object) -> float | None:
    try:
        return float(state)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None  # unavailable, unknown, or not a number


def fetch(
    hass: HomeAssistant, entity_ids: Iterable[str], start_ts: float, end_ts: float
) -> dict[str, Series]:
    """Every recorded change of the entities between two timestamps, as Series.

    Uses the recorder's history API, so it works with SQLite, MariaDB and PostgreSQL.
    Must be called from the recorder executor (recorder.get_instance(hass).async_add_executor_job).
    """
    ids = sorted(set(entity_ids))
    if not ids:
        return {}
    rows = history.get_significant_states(
        hass,
        dt_util.utc_from_timestamp(start_ts),
        dt_util.utc_from_timestamp(end_ts),
        entity_ids=ids,
        include_start_time_state=True,
        significant_changes_only=False,
        minimal_response=True,
        no_attributes=True,
        compressed_state_format=True,
    )
    out: dict[str, Series] = {}
    for entity_id, states in rows.items():
        samples: list[tuple[float, float | None]] = []
        for row in states:
            if isinstance(row, dict):
                ts, value = row.get("lu"), _number(row.get("s"))
            else:  # a State object
                ts, value = row.last_updated_timestamp, _number(row.state)
            if ts is not None:
                samples.append((float(ts), value))
        out[entity_id] = Series(samples)
    return out
