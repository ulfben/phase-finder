"""Find which mains phase a load is on, from recorded power history.

Pure Python with no Home Assistant imports, so it can be tested and replayed on its own.

The idea: when a load's power steps by Δ watts, the phase it is connected to steps by about Δ
at the same moment, and the other phases do not. Each clear step is one vote. Votes add up
over many steps and runs, so a few coincidences (another load switching at the same time)
are outvoted.

A phase *meter* gives one reading per phase, built from one or more sensors per phase
(for example a P1 meter's import minus its export). Several meters can be configured. Each
step uses the first meter whose readings on all three phases are fresh around the step.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

PHASES: tuple[str, ...] = ("L1", "L2", "L3")
UNKNOWN = "unknown"


def phase_key(phases: Iterable[str]) -> str:
    """'L2+L3' for a set of phases, in L1-L2-L3 order."""
    chosen = set(phases)
    return "+".join(p for p in PHASES if p in chosen)


def key_phases(key: str) -> tuple[str, ...]:
    """The phases in a vote key such as 'L1' or 'L2+L3'."""
    return tuple(key.split("+")) if key and key != UNKNOWN else ()


ALL_KEYS: tuple[str, ...] = (
    "L1",
    "L2",
    "L3",
    "L1+L2",
    "L1+L3",
    "L2+L3",
    "L1+L2+L3",
)


class Series:
    """Samples of one sensor in time order. A value of None means unavailable."""

    __slots__ = ("ts", "values")

    def __init__(self, samples: Iterable[tuple[float, float | None]] = ()) -> None:
        pairs = sorted(samples, key=lambda s: s[0])
        self.ts: list[float] = [t for t, _ in pairs]
        self.values: list[float | None] = [v for _, v in pairs]

    def __len__(self) -> int:
        return len(self.ts)

    def index_at(self, t: float) -> int:
        """Index of the last sample at or before t, or -1."""
        return bisect.bisect_right(self.ts, t) - 1

    def value_at(self, t: float) -> float | None:
        i = self.index_at(t)
        return self.values[i] if i >= 0 else None

    def has_sample_between(self, start: float, end: float) -> bool:
        """True if a sample lies in the half-open interval (start, end]."""
        i = self.index_at(end)
        return i >= 0 and self.ts[i] > start


@dataclass(frozen=True)
class Term:
    """One sensor's contribution to a phase reading, in watts after scale and sign."""

    source: str
    scale: float = 1.0  # to watts: 1000 for kW, or the nominal voltage for a current in A
    sign: float = 1.0  # -1 for an export sensor that is subtracted


@dataclass(frozen=True)
class Meter:
    """Readings per phase. Each phase is the sum of its terms."""

    name: str
    phases: Mapping[str, Sequence[Term]]
    # The readings have no sign (a current: importing and exporting 3 A look the same), so a
    # step may show as +delta or -delta on its phase. Such a meter votes for single phases only.
    unsigned: bool = False

    def sources(self) -> set[str]:
        return {term.source for terms in self.phases.values() for term in terms}


@dataclass(frozen=True)
class Params:
    """Tuning. The defaults were tuned on a Swedish 3-phase house with a P1 meter."""

    min_step_w: float = 40.0  # smaller steps drown in the other loads' noise
    window_s: float = 12.0  # compare the phases this long before and after the step
    merge_s: float = 6.0  # samples this close, in the same direction, form one step
    lead_s: float = 5.0  # a phase sample this long before the step still counts as fresh
    uncertainty_s: float = 0.0  # widen the "before" side by up to this when the last sample is old
    require_fresh: bool = True  # skip a step unless every phase has a sample around it
    match_ratio: float = 0.35  # the voted phase's error must be below this share of |Δ|
    reject_ratio: float = 0.7  # every other candidate's error must be above this share
    multi_min_share: float = 0.25  # a phase that takes part in a multi-phase step


@dataclass(frozen=True)
class Step:
    """A change in a load's power: delta watts between t_before and t_after."""

    t_start: float  # first sample of the change
    t_end: float  # last sample of the change (same as t_start for a single jump)
    t_prev: float  # the sample before the change
    delta: float


@dataclass
class LoadResult:
    """What one analysis run found for one load."""

    steps: int = 0  # steps big enough to test
    tested: int = 0  # steps with fresh meter readings
    votes: dict[str, int] = field(default_factory=dict)
    last_vote_ts: float | None = None
    meters_used: dict[str, int] = field(default_factory=dict)
    # Per other meter: 'L1>L3' counts, its own vote against the reference meter's.
    cross: dict[str, dict[str, int]] = field(default_factory=dict)


def find_steps(series: Series, params: Params) -> list[Step]:
    """Power steps of at least min_step_w, merging quick same-direction changes."""
    steps: list[Step] = []
    prev_t: float | None = None
    prev_v: float | None = None
    # The step being built: start, end, sample before it, accumulated delta.
    cur: list[float] | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and abs(cur[3]) >= params.min_step_w:
            steps.append(Step(t_start=cur[0], t_end=cur[1], t_prev=cur[2], delta=cur[3]))
        cur = None

    for t, v in zip(series.ts, series.values, strict=True):
        if v is None:
            flush()
            prev_t = prev_v = None
            continue
        if prev_v is None or prev_t is None:
            prev_t, prev_v = t, v
            continue
        d = v - prev_v
        if d != 0:
            same_way = cur is not None and (d > 0) == (cur[3] > 0)
            if same_way and cur is not None and t - cur[1] <= params.merge_s:
                cur[1] = t
                cur[3] += d
            else:
                flush()
                cur = [t, t, prev_t, d]
        prev_t, prev_v = t, v
    flush()
    return steps


def _phase_reading(terms: Sequence[Term], data: Mapping[str, Series], t: float) -> float | None:
    total = 0.0
    for term in terms:
        series = data.get(term.source)
        if series is None:
            return None
        value = series.value_at(t)
        if value is None:
            return None
        total += term.sign * term.scale * value
    return total


def _phase_fresh(
    terms: Sequence[Term], data: Mapping[str, Series], start: float, end: float
) -> bool:
    return any(
        (series := data.get(term.source)) is not None and series.has_sample_between(start, end)
        for term in terms
    )


def phase_deltas(
    step: Step, meter: Meter, data: Mapping[str, Series], params: Params
) -> dict[str, float] | None:
    """How much each phase changed across the step, or None if the meter can't tell."""
    gap = step.t_start - step.t_prev
    before = step.t_start - params.window_s - min(max(gap, 0.0), params.uncertainty_s)
    after = step.t_end + params.window_s
    deltas: dict[str, float] = {}
    for phase in PHASES:
        terms = meter.phases.get(phase)
        if not terms:
            return None
        if params.require_fresh and not _phase_fresh(
            terms, data, step.t_start - params.lead_s, after
        ):
            return None
        a = _phase_reading(terms, data, before)
        b = _phase_reading(terms, data, after)
        if a is None or b is None:
            return None
        deltas[phase] = b - a
    return deltas


def classify(
    delta: float, deltas: Mapping[str, float], params: Params, unsigned: bool = False
) -> str | None:
    """The phase (or phases) that explain a step, or None if it is not clear.

    A single phase wins when its change matches the step and no other candidate comes close.
    A multi-phase load (a 400 V heater across two phases, a 3-phase motor) changes several
    phases by part of the step each; that is a vote for the combination.
    """
    size = abs(delta)
    match = params.match_ratio * size
    reject = params.reject_ratio * size
    if unsigned:
        single = {p: min(abs(deltas[p] - delta), abs(deltas[p] + delta)) for p in PHASES}
    else:
        single = {p: abs(deltas[p] - delta) for p in PHASES}
    ranked = sorted(PHASES, key=single.__getitem__)
    if single[ranked[0]] < match:
        # One phase fits. It counts only if the runner-up is clearly worse.
        return ranked[0] if single[ranked[1]] > reject else None
    if unsigned:
        return None  # a split over phases can't be told without the sign
    # No single phase explains the step: try a split over two or three phases. Each phase in
    # the split must carry a real part of it, and the phases outside it must stay still.
    split: dict[str, float] = {}
    for key in ALL_KEYS[len(PHASES) :]:
        phases = key_phases(key)
        if min(deltas[p] / delta for p in phases) < params.multi_min_share:
            continue
        if any(abs(deltas[p]) >= match for p in PHASES if p not in phases):
            continue
        split[key] = abs(sum(deltas[p] for p in phases) - delta)
    if not split:
        return None
    best = min(split, key=split.__getitem__)
    if split[best] >= match:
        return None
    if any(err <= reject for key, err in split.items() if key != best):
        return None
    return best


def translate(key: str, mapping: Mapping[str, str]) -> str:
    """A vote in a meter's own phase labels, in the reference meter's labels."""
    return phase_key(mapping[p] for p in key_phases(key))


def analyse_load(
    load: str,
    meters: Sequence[Meter],
    data: Mapping[str, Series],
    params: Params,
    start: float | None = None,
    end: float | None = None,
    mappings: Mapping[str, Mapping[str, str]] | None = None,
    order: Sequence[str] | None = None,
) -> LoadResult:
    """Vote over the load's steps that start in [start, end).

    meters[0] is the reference: its labels are the answer. Another meter votes only once
    its mapping to the reference labels is known (mappings[name]); until then it is only
    compared with the reference, to learn that mapping (LoadResult.cross). order lists the
    meter names to try for votes, best first (the default is the configured order).
    """
    result = LoadResult()
    series = data.get(load)
    if series is None or not meters:
        return result
    by_name = {m.name: m for m in meters}
    reference = meters[0].name
    known: dict[str, Mapping[str, str]] = {reference: {p: p for p in PHASES}}
    for name, mapping in (mappings or {}).items():
        if name in by_name and name != reference:
            known[name] = mapping
    vote_order = [n for n in (order or [m.name for m in meters]) if n in known]
    for name in known:
        if name not in vote_order:
            vote_order.append(name)
    for step in find_steps(series, params):
        if start is not None and step.t_start < start:
            continue
        if end is not None and step.t_start >= end:
            continue
        result.steps += 1
        keys: dict[str, str | None] = {}
        fresh: set[str] = set()
        for meter in meters:
            deltas = phase_deltas(step, meter, data, params)
            if deltas is None:
                continue
            fresh.add(meter.name)
            keys[meter.name] = classify(step.delta, deltas, params, meter.unsigned)
        if not fresh:
            continue
        result.tested += 1
        ref_key = keys.get(reference)
        if ref_key in PHASES:
            for name, key in keys.items():
                if name != reference and key in PHASES:
                    pair = f"{key}>{ref_key}"
                    tally = result.cross.setdefault(name, {})
                    tally[pair] = tally.get(pair, 0) + 1
        for name in vote_order:
            if name not in fresh:
                continue
            result.meters_used[name] = result.meters_used.get(name, 0) + 1
            key = keys[name]
            if key is not None:
                key = translate(key, known[name])
                result.votes[key] = result.votes.get(key, 0) + 1
                result.last_vote_ts = step.t_start
            break
    return result


def learn_mapping(
    tally: Mapping[str, float], min_votes: float = 10.0, min_share: float = 0.85
) -> dict[str, str] | None:
    """A meter's labels in the reference meter's labels, from steps both meters saw.

    tally counts 'L1>L3' for "this meter said L1 where the reference said L3". The mapping
    must be clear for every phase and use each reference phase once.
    """
    mapping: dict[str, str] = {}
    for own in PHASES:
        counts = {ref: tally.get(f"{own}>{ref}", 0.0) for ref in PHASES}
        total = sum(counts.values())
        best = max(PHASES, key=counts.__getitem__)
        if total < min_votes or counts[best] / total < min_share:
            return None
        mapping[own] = best
    if len(set(mapping.values())) != len(PHASES):
        return None
    return mapping


# --- Turning votes into an answer -----------------------------------------------------------


@dataclass(frozen=True)
class VerdictParams:
    min_votes: float = 4.0  # fewer votes than this give no answer yet
    # A phase takes part in a multi-phase load (a washing machine with its heater on one phase
    # and its motor on another) when it has this share of the votes and this many votes.
    involved_share: float = 0.3
    involved_votes: float = 10.0
    multi_share: float = 0.5  # multi-phase steps that make up this share of the votes
    certain_confidence: float = 0.8  # below this the answer is shown as a guess
    certain_votes: float = 8.0
    switch_confidence: float = 0.75  # a published answer changes only for a clearer one


@dataclass(frozen=True)
class Verdict:
    phase: str | None  # 'L1', 'L2+L3', ... or None when unknown
    confidence: float  # 0..1, shrunk towards 0 when there are few votes
    share: float  # share of votes that agree with the phase
    total: float

    @property
    def phases(self) -> tuple[str, ...]:
        return key_phases(self.phase or "")


def _confidence(agree: float, total: float) -> float:
    # A Laplace-style estimate: 2 of 2 votes gives 0.6, 20 of 20 gives 0.91.
    return (agree + 1.0) / (total + 3.0)


def verdict(votes: Mapping[str, float], params: VerdictParams | None = None) -> Verdict:
    """The best answer the votes support, without hysteresis.

    Multi-phase is the answer only when most votes are multi-phase steps (a 400 V heater
    changes two phases at once), or when two phases each take a large part of the
    single-phase votes. Single-phase votes split over two phases far more often mean the
    load was moved, which detect_move handles before the votes get here.
    """
    p = params or VerdictParams()
    total = float(sum(votes.values()))
    if total <= 0 or total < p.min_votes:
        return Verdict(None, 0.0, 0.0, total)
    top_key = max(votes, key=votes.__getitem__)
    if len(key_phases(top_key)) > 1 and votes[top_key] / total >= p.multi_share:
        agree = votes[top_key]
        return Verdict(top_key, _confidence(agree, total), agree / total, total)
    singles = {ph: votes.get(ph, 0.0) for ph in PHASES}
    single_total = sum(singles.values())
    if single_total <= 0:
        return Verdict(None, 0.0, 0.0, total)
    involved = [
        ph
        for ph in PHASES
        if singles[ph] / single_total >= p.involved_share and singles[ph] >= p.involved_votes
    ]
    if len(involved) >= 2:
        key = phase_key(involved)
        agree = sum(c for k, c in votes.items() if set(key_phases(k)) <= set(involved))
        return Verdict(key, _confidence(agree, total), agree / total, total)
    # A single phase. Stray multi-phase votes (another load switching at the same moment)
    # don't count against it; with a low share it is only a guess, which the confidence shows.
    top = max(PHASES, key=singles.__getitem__)
    agree = singles[top]
    return Verdict(top, _confidence(agree, single_total), agree / single_total, total)


def _single_share(votes: Mapping[str, float]) -> tuple[str | None, float, float]:
    """(top single phase, its share of the single-phase votes, single-phase vote count)."""
    singles = {ph: votes.get(ph, 0.0) for ph in PHASES}
    total = sum(singles.values())
    if total <= 0:
        return None, 0.0, 0.0
    top = max(PHASES, key=singles.__getitem__)
    return top, singles[top] / total, total


def detect_move(
    buckets: Sequence[Mapping[str, float]],
    min_votes: float = 10.0,
    new_share: float = 0.75,
    old_share: float = 0.6,
) -> int | None:
    """Where a load moved to another phase, as the index of the first bucket after the move.

    buckets are vote counts per period (a day), oldest first. A move is a split where the
    newer buckets agree on one phase, the older ones agreed on another, and both sides have
    enough single-phase votes. The earliest such split wins, so the new side keeps as much
    evidence as possible. None when there is no clear move.
    """
    for i in range(1, len(buckets)):
        older: dict[str, float] = {}
        for bucket in buckets[:i]:
            older = merge_votes(older, bucket)
        newer: dict[str, float] = {}
        for bucket in buckets[i:]:
            newer = merge_votes(newer, bucket)
        new_top, new_frac, new_n = _single_share(newer)
        old_top, old_frac, old_n = _single_share(older)
        if new_top is None or old_top is None or new_top == old_top:
            continue
        if new_n < min_votes or old_n < min_votes:
            continue
        if new_frac >= new_share and old_frac >= old_share:
            return i
    return None


def publish(current: str | None, new: Verdict, params: VerdictParams | None = None) -> str | None:
    """The phase to show, given what is shown now. Keeps weak results from flip-flopping."""
    p = params or VerdictParams()
    if new.phase is None:
        return current
    if current is None or new.phase == current:
        return new.phase
    return new.phase if new.confidence >= p.switch_confidence else current


def is_certain(v: Verdict, params: VerdictParams | None = None) -> bool:
    p = params or VerdictParams()
    return (
        v.phase is not None
        and v.confidence >= p.certain_confidence
        and (v.total >= p.certain_votes)
    )


def decay(votes: Mapping[str, float], elapsed_s: float, half_life_s: float) -> dict[str, float]:
    """Fade old votes so a load that moves to another socket is found again."""
    if half_life_s <= 0 or elapsed_s <= 0:
        return dict(votes)
    factor = 0.5 ** (elapsed_s / half_life_s)
    return {k: v * factor for k, v in votes.items() if v * factor >= 0.01}


def merge_votes(old: Mapping[str, float], new: Mapping[str, int | float]) -> dict[str, float]:
    merged = dict(old)
    for key, count in new.items():
        merged[key] = merged.get(key, 0.0) + count
    return merged
