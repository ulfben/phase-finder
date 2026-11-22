"""The analysis runs: schedule, history chunks, saved votes and published phases."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from . import analysis as an
from .const import (
    CHUNK_S,
    CONF_EXCLUDE,
    CONF_HALF_LIFE_DAYS,
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
    EVENT_PHASE_CHANGED,
    EXPORT_FIELDS,
    FIRST_RUN_DELAY_S,
    PHASE_FIELDS,
    POWER_UNITS,
    PRE_S,
    RUN_HOUR,
    RUN_MINUTE,
    SIGNAL_UPDATED,
    STORAGE_VERSION,
)
from .discovery import find_loads
from .history import fetch

_LOGGER = logging.getLogger(__name__)
DAY_S = 86400.0


@dataclass
class LoadState:
    """What is known about one load, saved between runs."""

    key: str  # entity registry id, stable across renames
    entity_id: str
    votes: dict[str, float] = field(default_factory=dict)
    published: str | None = None
    steps: float = 0.0
    tested: float = 0.0
    last_vote_ts: float | None = None
    # Raw vote counts per analysed period (a day), oldest first: [start_ts, votes].
    history: list[list[Any]] = field(default_factory=list)

    @property
    def verdict(self) -> an.Verdict:
        return an.verdict(self.votes)

    def forget(self) -> None:
        self.votes, self.history, self.published = {}, [], None
        self.steps, self.tested, self.last_vote_ts = 0.0, 0.0, None

    def update(
        self, buckets: list[tuple[float, dict[str, int]]], now: float, half_life: float
    ) -> bool:
        """Add new per-period votes, drop the ones before a move, and recompute the faded
        total. Returns True if the load moved."""
        self.history.extend([ts, dict(v)] for ts, v in buckets if v)
        self.history.sort(key=lambda b: b[0])
        if half_life > 0:  # after eight half-lives a vote counts less than 0.4 %
            self.history = [b for b in self.history if now - b[0] < 8 * half_life]
        moved_at = an.detect_move([b[1] for b in self.history])
        if moved_at is not None:
            self.history = self.history[moved_at:]
        votes: dict[str, float] = {}
        for ts, bucket in self.history:
            factor = 0.5 ** (max(now - ts, 0.0) / half_life) if half_life > 0 else 1.0
            for key, n in bucket.items():
                votes[key] = votes.get(key, 0.0) + n * factor
        self.votes = {k: v for k, v in votes.items() if v >= 0.01}
        return moved_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "votes": self.votes,
            "published": self.published,
            "steps": self.steps,
            "tested": self.tested,
            "last_vote_ts": self.last_vote_ts,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, key: str, data: Mapping[str, Any]) -> LoadState:
        return cls(
            key=key,
            entity_id=data["entity_id"],
            votes={k: float(v) for k, v in (data.get("votes") or {}).items()},
            published=data.get("published"),
            steps=float(data.get("steps") or 0),
            tested=float(data.get("tested") or 0),
            last_vote_ts=data.get("last_vote_ts"),
            history=[list(b) for b in data.get("history") or []],
        )


@dataclass
class _Totals:
    steps: int = 0
    tested: int = 0
    votes: dict[str, int] = field(default_factory=dict)
    last_vote_ts: float | None = None
    buckets: list[tuple[float, dict[str, int]]] = field(default_factory=list)

    def add(self, start: float, result: an.LoadResult) -> None:
        self.buckets.append((start, dict(result.votes)))
        self.steps += result.steps
        self.tested += result.tested
        for key, count in result.votes.items():
            self.votes[key] = self.votes.get(key, 0) + count
        if result.last_vote_ts is not None:
            self.last_vote_ts = max(self.last_vote_ts or 0.0, result.last_vote_ts)


class PhaseFinder:
    """Owns the saved votes and runs the analysis."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self.loads: dict[str, LoadState] = {}
        self.analysed_until: float | None = None
        self.cross: dict[str, dict[str, float]] = {}
        self.mappings: dict[str, dict[str, str]] = {}
        self.last_run: dict[str, Any] | None = None
        self.running = False
        self._known_keys: set[str] = set()
        self._new_load_listeners: list[Callable[[list[LoadState]], None]] = []

    # --- configuration ---------------------------------------------------------------------

    @property
    def options(self) -> Mapping[str, Any]:
        return self.entry.options

    @property
    def meter_options(self) -> list[dict[str, Any]]:
        return list(self.options.get(CONF_METERS) or [])

    def params(self) -> an.Params:
        return an.Params(
            min_step_w=float(self.options.get(CONF_MIN_STEP_W, DEFAULT_MIN_STEP_W)),
            window_s=float(self.options.get(CONF_WINDOW_S, DEFAULT_WINDOW_S)),
        )

    def _scale(self, entity_id: str) -> float:
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return POWER_UNITS.get(unit, 1.0)

    def meters(self) -> list[an.Meter]:
        """The configured meters, reference first. A meter's name is its L1 entity id."""
        meters: list[an.Meter] = []
        for option in self.meter_options:
            sign = -1.0 if option.get(CONF_INVERT) else 1.0
            phases: dict[str, list[an.Term]] = {}
            for phase, imp, exp in zip(an.PHASES, PHASE_FIELDS, EXPORT_FIELDS, strict=True):
                terms = [an.Term(option[imp], self._scale(option[imp]), sign)]
                if option.get(exp):
                    terms.append(an.Term(option[exp], self._scale(option[exp]), -sign))
                phases[phase] = terms
            meters.append(an.Meter(option["l1"], phases))
        return meters

    def meter_label(self, name: str) -> str:
        for option in self.meter_options:
            if option["l1"] == name:
                return option.get(CONF_NAME) or name
        return name

    def load_keys(self) -> dict[str, str]:
        """entity_id -> storage key for the loads to analyse now."""
        ent_reg = er.async_get(self.hass)
        keys: dict[str, str] = {}
        for entity_id in find_loads(self.hass, self.options, self.meter_options):
            entry = ent_reg.async_get(entity_id)
            keys[entity_id] = entry.id if entry else f"entity:{entity_id}"
        return keys

    # --- lifecycle -------------------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.analysed_until = data.get("analysed_until")
        self.cross = data.get("cross") or {}
        self.mappings = data.get("mappings") or {}
        self.last_run = data.get("last_run")
        self.loads = {k: LoadState.from_dict(k, v) for k, v in (data.get("loads") or {}).items()}
        self.sync_loads()

    def sync_loads(self) -> list[LoadState]:
        """Track the loads the options select now. Returns the ones that are new."""
        ent_reg = er.async_get(self.hass)
        new: list[LoadState] = []
        for entity_id, key in self.load_keys().items():
            load = self.loads.get(key)
            if load is None:
                load = self.loads[key] = LoadState(key, entity_id)
            elif load.entity_id != entity_id:
                load.entity_id = entity_id  # renamed
            if key not in self._known_keys:
                self._known_keys.add(key)
                new.append(load)
        # A stored load whose entity was renamed while we were not running.
        for load in self.loads.values():
            entry = ent_reg.async_get(load.key) if not load.key.startswith("entity:") else None
            if entry is not None:
                load.entity_id = entry.entity_id
        return new

    def active_keys(self) -> set[str]:
        return set(self.load_keys().values())

    def mark_known(self, keys: set[str]) -> None:
        """These loads have entities already; don't announce them as new."""
        self._known_keys |= keys

    def sensor_keys(self) -> set[str]:
        """Loads that get a phase sensor: the active ones, plus known loads whose source still
        exists and is not excluded (an integration that is slow to start must not lose its
        loads' sensors)."""
        ent_reg = er.async_get(self.hass)
        exclude = set(self.options.get(CONF_EXCLUDE) or ())
        keys = self.active_keys()
        for key, load in self.loads.items():
            if key in keys or load.entity_id in exclude:
                continue
            if key.startswith("entity:") or ent_reg.async_get(key) is not None:
                keys.add(key)
        return keys

    async def async_start(self) -> None:
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._scheduled, hour=RUN_HOUR, minute=RUN_MINUTE, second=0
            )
        )
        if self.analysed_until is None:

            @callback
            def _started(_hass: HomeAssistant) -> None:
                self._unsubs.append(async_call_later(self.hass, FIRST_RUN_DELAY_S, self._first_run))

            self._unsubs.append(async_at_started(self.hass, _started))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _first_run(self, _now: datetime) -> None:
        self.entry.async_create_background_task(self.hass, self.async_run(), f"{DOMAIN} first run")

    @callback
    def _scheduled(self, _now: datetime) -> None:
        interval = float(self.options.get(CONF_INTERVAL_DAYS, DEFAULT_INTERVAL_DAYS)) * DAY_S
        last = (self.last_run or {}).get("finished_ts")
        if last is not None and time.time() - last < interval - DAY_S / 2:
            return
        self.entry.async_create_background_task(self.hass, self.async_run(), f"{DOMAIN} run")

    @callback
    def async_add_new_load_listener(
        self, listener: Callable[[list[LoadState]], None]
    ) -> CALLBACK_TYPE:
        self._new_load_listeners.append(listener)
        return lambda: self._new_load_listeners.remove(listener)

    # --- running ---------------------------------------------------------------------------

    async def async_run(self, full: bool = False) -> None:
        """Analyse the history recorded since the last run (or all of it, with full)."""
        if self._lock.locked():
            _LOGGER.debug("An analysis is already running")
            return
        async with self._lock:
            self.running = True
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATED}_{self.entry.entry_id}")
            try:
                await self._async_run(full)
            finally:
                self.running = False
                async_dispatcher_send(self.hass, f"{SIGNAL_UPDATED}_{self.entry.entry_id}")

    async def _async_run(self, full: bool) -> None:
        started = time.time()
        new = self.sync_loads()
        if new:
            for listener in list(self._new_load_listeners):
                listener(new)
        meters = self.meters()
        if not meters:
            _LOGGER.warning("No phase meter is configured")
            return
        if full:
            for load in self.loads.values():
                load.votes, load.history = {}, []
                load.steps, load.tested, load.last_vote_ts = 0.0, 0.0, None
            self.cross, self.mappings, self.analysed_until = {}, {}, None
        params = self.params()
        recorder = get_instance(self.hass)
        now = time.time()
        oldest = now - recorder.keep_days * DAY_S + 3600.0
        start = max(self.analysed_until or oldest, oldest)
        end = now - params.window_s - 60.0
        active = {key: self.loads[key] for key in self.active_keys() if key in self.loads}
        if end <= start or not active:
            return
        reference = meters[0].name
        unmapped = [m.name for m in meters[1:] if m.name not in self.mappings]

        totals, cross, samples = await self._async_analyse(meters, active, params, start, end)
        self._add_cross(cross)
        learned = self._learn_mappings(meters)
        if unmapped and any(name in learned for name in unmapped):
            # A meter's labels became known: analyse again so its readings vote this time.
            _LOGGER.debug("Learned phase mappings %s; analysing again", learned)
            totals, _, samples = await self._async_analyse(meters, active, params, start, end)

        half_life = float(self.options.get(CONF_HALF_LIFE_DAYS, DEFAULT_HALF_LIFE_DAYS)) * DAY_S
        changes: list[tuple[LoadState, str | None, str | None, bool]] = []
        run_votes = 0
        for key, load in active.items():
            if load.votes and not load.history:
                # Saved by a version without per-day history: keep it as one period.
                load.history = [[self.analysed_until or start, dict(load.votes)]]
            total = totals.get(key) or _Totals()
            moved = load.update(total.buckets, end, half_life)
            load.steps += total.steps
            load.tested += total.tested
            if total.last_vote_ts is not None:
                load.last_vote_ts = total.last_vote_ts
            run_votes += sum(total.votes.values())
            before = load.published
            new = load.verdict
            # After a move the old answer is known to be wrong: show the new one right away.
            load.published = new.phase if moved and new.phase else an.publish(before, new)
            if load.published != before or moved:
                changes.append((load, before, load.published, moved))
        self.analysed_until = end
        self.last_run = {
            "started_ts": started,
            "finished_ts": time.time(),
            "duration_s": round(time.time() - started, 1),
            "from_ts": start,
            "to_ts": end,
            "loads": len(active),
            "steps": sum(t.steps for t in totals.values()),
            "tested": sum(t.tested for t in totals.values()),
            "votes": run_votes,
            "meters": {
                m.name: {
                    "name": self.meter_label(m.name),
                    "reference": m.name == reference,
                    "samples": samples.get(m.name, 0),
                    "mapping": None if m.name == reference else self.mappings.get(m.name),
                }
                for m in meters
            },
        }
        await self._async_save()
        for load, old, new_phase, moved in changes:
            if moved:
                _LOGGER.info("%s seems to have moved: %s -> %s", load.entity_id, old, new_phase)
            self.hass.bus.async_fire(
                EVENT_PHASE_CHANGED,
                {
                    "entity_id": load.entity_id,
                    "old_phase": old,
                    "new_phase": new_phase,
                    "moved": moved,
                },
            )
        _LOGGER.info(
            "Analysed %.1f days of history for %d loads: %d votes in %.1f s",
            (end - start) / DAY_S,
            len(active),
            run_votes,
            self.last_run["duration_s"],
        )

    async def _async_analyse(
        self,
        meters: list[an.Meter],
        loads: Mapping[str, LoadState],
        params: an.Params,
        start: float,
        end: float,
    ) -> tuple[dict[str, _Totals], dict[str, dict[str, int]], dict[str, int]]:
        recorder = get_instance(self.hass)
        mappings = dict(self.mappings)
        totals: dict[str, _Totals] = {}
        cross: dict[str, dict[str, int]] = {}
        samples: dict[str, int] = {}
        by_entity = {load.entity_id: key for key, load in loads.items()}
        a = start
        while a < end:
            b = min(a + CHUNK_S, end)
            results, counts = await recorder.async_add_executor_job(
                _analyse_chunk, self.hass, meters, list(by_entity), params, a, b, mappings
            )
            for entity_id, result in results.items():
                totals.setdefault(by_entity[entity_id], _Totals()).add(a, result)
                for meter, tally in result.cross.items():
                    into = cross.setdefault(meter, {})
                    for pair, n in tally.items():
                        into[pair] = into.get(pair, 0) + n
            for name, n in counts.items():
                samples[name] = samples.get(name, 0) + n
            a = b
        return totals, cross, samples

    def _add_cross(self, cross: Mapping[str, Mapping[str, int]]) -> None:
        for meter, tally in cross.items():
            into = self.cross.setdefault(meter, {})
            for pair, n in tally.items():
                into[pair] = into.get(pair, 0.0) + n

    def _learn_mappings(self, meters: list[an.Meter]) -> dict[str, dict[str, str]]:
        """Update the other meters' phase mappings from the comparisons so far."""
        learned: dict[str, dict[str, str]] = {}
        for meter in meters[1:]:
            mapping = an.learn_mapping(self.cross.get(meter.name, {}))
            if mapping is not None and mapping != self.mappings.get(meter.name):
                self.mappings[meter.name] = mapping
                learned[meter.name] = mapping
                if mapping != {p: p for p in an.PHASES}:
                    _LOGGER.info(
                        "%s labels its phases differently from the reference meter: %s",
                        self.meter_label(meter.name),
                        ", ".join(f"its {k} is {v}" for k, v in mapping.items()),
                    )
        return learned

    async def async_forget(self, entity_ids: list[str]) -> list[str]:
        """Forget what is known about loads, given their phase sensors or power sensors."""
        ent_reg = er.async_get(self.hass)
        prefix = f"{self.entry.entry_id}_"
        forgotten: list[str] = []
        for entity_id in entity_ids:
            reg = ent_reg.async_get(entity_id)
            key = None
            if reg is not None and reg.platform == DOMAIN and reg.unique_id.startswith(prefix):
                key = reg.unique_id.removeprefix(prefix)
            else:
                key = next((k for k, v in self.loads.items() if v.entity_id == entity_id), None)
            load = self.loads.get(key) if key else None
            if load is not None:
                load.forget()
                forgotten.append(load.entity_id)
        if forgotten:
            await self._async_save()
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATED}_{self.entry.entry_id}")
        return forgotten

    async def _async_save(self) -> None:
        await self._store.async_save(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysed_until": self.analysed_until,
            "cross": self.cross,
            "mappings": self.mappings,
            "last_run": self.last_run,
            "loads": {k: v.as_dict() for k, v in self.loads.items()},
        }


def _analyse_chunk(
    hass: HomeAssistant,
    meters: list[an.Meter],
    loads: list[str],
    params: an.Params,
    start: float,
    end: float,
    mappings: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, an.LoadResult], dict[str, int]]:
    """Read one chunk of history and vote. Runs in the recorder executor."""
    sources = set(loads)
    for meter in meters:
        sources |= meter.sources()
    data = fetch(hass, sources, start - PRE_S, end + params.window_s + 60.0)
    counts: dict[str, int] = {}
    for meter in meters:
        # A meter is as fresh as its slowest phase.
        counts[meter.name] = min(
            sum(len(data[t.source]) for t in terms if t.source in data)
            for terms in meter.phases.values()
        )
    order = sorted(counts, key=counts.__getitem__, reverse=True)
    results = {
        load: an.analyse_load(load, meters, data, params, start, end, mappings, order)
        for load in loads
    }
    return results, counts
