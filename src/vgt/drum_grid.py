"""Reconcile a drum backend's own quantized timeline with vgt's beat grid.

DrumScript emits absolute-second onsets, but they are quantized to a grid it
derives from *its own* beat tracker: on 7Rivers every event lands on a
multiple of 0.249615 s anchored at exactly 0.0, while the project's analyzed
grid is 0.249992 s anchored at its 0.085333 s downbeat. Authoring those times
verbatim therefore starts the reference MIDI at the item edge instead of the
first beat, and the 0.15% rate difference accumulates into a whole eighth
note of drift by the end of a three-minute song.

Both grids describe the same performance, so the fix is to move each event to
the nearest line of vgt's grid, at the subdivision the backend was using.
Nothing musical is lost, because a fully quantized backend carries no
micro-timing to preserve.

Counting subdivisions instead -- re-emitting the backend's *index* on vgt's
grid -- is tempting and wrong: the two grids disagree about how many
subdivisions have elapsed (0.15% on 7Rivers is a whole eighth note across the
song), so from the point where that disagreement passes half a subdivision
every note lands one slot out. Measured against the stem's onsets, index
mapping leaves 92 of 410 events more than 60 ms from any real hit; snapping
leaves 6.

Every step is guarded: a backend whose onsets are *not* on a uniform grid
(a future unquantized backend, whose real-second onsets need no correction
and whose feel snapping would destroy), a beat grid that starts after the
events, or a subdivision that disagrees with the project tempo all leave the
events untouched rather than guessing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .drum_cleanup import BeatGridReference


# Below this many distinct onsets a "uniform grid" fit is not evidence of
# anything -- four events fit almost any step.
MIN_EVENTS_FOR_FIT = 8
# Largest deviation from the fitted grid, as a fraction of one step, still
# read as "this backend quantizes". Real DrumScript output sits at ~0.6%.
STEP_RESIDUAL_TOLERANCE = 0.15
# How far the backend's implied subdivision may be from the project's before
# the two are treated as describing different grids.
TEMPO_AGREEMENT_TOLERANCE = 0.05


@dataclass(frozen=True)
class GridReconciliation:
    """What `reconcile_event_times` did, for logging and tests."""

    step_seconds: float  # the backend's own detected grid step
    subdivisions_per_beat: int  # project subdivisions matched to that step
    median_shift_seconds: float
    max_shift_seconds: float

    def describe(self) -> str:
        return (
            f"snapped to the project beat grid "
            f"(backend step {self.step_seconds * 1000:.1f} ms, "
            f"1/{self.subdivisions_per_beat} beat, "
            f"median shift {self.median_shift_seconds * 1000:+.0f} ms, "
            f"max {self.max_shift_seconds * 1000:.0f} ms)"
        )


def _fit_step(step: float, times: Sequence[float]) -> float | None:
    """Least-squares refine a candidate step against the indices it implies,
    or None if the times are not multiples of it within tolerance."""
    for _ in range(3):
        indices = [round(time / step) for time in times]
        denominator = sum(index * index for index in indices)
        if denominator == 0:
            return None
        step = sum(index * time for index, time in zip(indices, times)) / denominator
        if step <= 0:
            return None
    indices = [round(time / step) for time in times]
    if max(abs(time - index * step) for index, time in zip(indices, times)) > STEP_RESIDUAL_TOLERANCE * step:
        return None
    return step


def detect_uniform_step(times: Sequence[float]) -> float | None:
    """Return the grid step every time in `times` is a multiple of, else None.

    The smallest gap between two events is one step only if some pair happens
    to be adjacent on the grid, so it is tried as itself and as a small
    multiple, coarsest first -- a finer step than the evidence requires would
    read gaps that are really rests as grid positions.

    The fit is through the origin: a backend that quantizes anchors its grid
    at its own time zero (DrumScript does), and one that does not will fail
    the residual check and be left alone, which is the safe outcome either
    way.
    """
    distinct = sorted({float(time) for time in times})
    if len(distinct) < MIN_EVENTS_FOR_FIT:
        return None
    gaps = [later - earlier for earlier, later in zip(distinct, distinct[1:]) if later - earlier > 1e-9]
    if not gaps:
        return None
    smallest_gap = min(gaps)
    for divisor in (1, 2, 3, 4):
        step = _fit_step(smallest_gap / divisor, distinct)
        if step is not None:
            return step
    return None


def _grid_beats(beat_grid: BeatGridReference) -> tuple[float, ...]:
    beats = {time for time in beat_grid.beat_times if time >= 0.0}
    if beat_grid.downbeat_offset_s is not None and beat_grid.downbeat_offset_s >= 0.0:
        beats.add(beat_grid.downbeat_offset_s)
    return tuple(sorted(beats))


def _subdivided(beats: Sequence[float], subdivisions: int) -> list[float]:
    """Beat times split into `subdivisions` equal parts each.

    Used only when no fitted tempo is available (see `reconcile_event_times`):
    each measured beat interval is subdivided separately, so a genuinely
    varying tempo is followed -- at the cost of following the beat tracker's
    per-beat noise too.
    """
    grid: list[float] = []
    for left, right in zip(beats, beats[1:]):
        span = right - left
        grid.extend(left + span * part / subdivisions for part in range(subdivisions))
    grid.append(beats[-1])
    return grid


def reconcile_event_times(
    events: Sequence[Mapping[str, Any]],
    *,
    beat_grid: BeatGridReference | None,
    beat_period_s: float | None = None,
) -> tuple[list[dict[str, Any]], GridReconciliation | None]:
    """Move `events` onto `beat_grid`, or return them unchanged.

    `beat_period_s` is the *fitted* beat period, passed when the tempo stage
    concluded the song runs at one constant tempo. Prefer it: the detected
    `beat_times` are individually noisy (on 7Rivers, eight of the 355 beats sit
    more than 50 ms off the fitted line, one by 140 ms), and placing notes on
    the raw array copies each of those local errors straight into the MIDI.
    The fitted line anchored at the analyzed downbeat is both smoother and
    closer to the audio, and it is the same grid REAPER's tempo map applies.
    Without it -- a piecewise grid, where the variation is the point -- the
    measured beats are subdivided as they came.

    Returns `(events, None)` -- copies, never the inputs -- whenever any
    precondition for a trustworthy correction is missing.
    """
    unchanged = [dict(event) for event in events]
    if beat_grid is None or not events:
        return unchanged, None
    beats = _grid_beats(beat_grid)
    if len(beats) < 2:
        return unchanged, None

    times = [float(event["time_sec"]) for event in events]
    step = detect_uniform_step(times)
    if step is None:
        return unchanged, None

    beat_period = beat_period_s if beat_period_s and beat_period_s > 0 else (beats[-1] - beats[0]) / (len(beats) - 1)
    subdivisions = round(beat_period / step)
    if subdivisions < 1 or abs(beat_period / (subdivisions * step) - 1.0) > TEMPO_AGREEMENT_TOLERANCE:
        return unchanged, None
    # Snapping needs a grid under every event, so an analysis that only found
    # beats from the middle of the song onwards is not usable.
    if beats[0] > min(times) + beat_period:
        return unchanged, None

    if beat_period_s and beat_period_s > 0:
        # One fitted tempo from the analyzed downbeat: defined everywhere, so
        # events past the last detected beat are covered too.
        anchor = beat_grid.downbeat_offset_s if beat_grid.downbeat_offset_s is not None else beats[0]
        target_step = beat_period_s / subdivisions
        nearest = lambda time: anchor + round((time - anchor) / target_step) * target_step  # noqa: E731
    else:
        grid = _subdivided(beats, subdivisions)
        tail_step = (grid[-1] - grid[0]) / (len(grid) - 1)

        def nearest(time: float) -> float:
            index = min(range(len(grid)), key=lambda position: abs(grid[position] - time))
            if index == len(grid) - 1 and time > grid[-1]:
                return grid[-1] + round((time - grid[-1]) / tail_step) * tail_step
            return grid[index]

    target_step = beat_period / subdivisions
    shifted: list[dict[str, Any]] = []
    shifts: list[float] = []
    for event, time in zip(events, times):
        aligned = nearest(time)
        # An event ahead of the first grid line snaps to the line before it,
        # which can fall before the recording starts; there is no such
        # position to author, so stay on the first line instead.
        while aligned < 0.0:
            aligned += target_step
        shifts.append(aligned - time)
        shifted.append({**event, "time_sec": aligned})

    ordered = sorted(shifts)
    midpoint = len(ordered) // 2
    median = ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
    max_shift = max(abs(shift) for shift in shifts)
    return shifted, GridReconciliation(
        step_seconds=step,
        subdivisions_per_beat=subdivisions,
        median_shift_seconds=median,
        max_shift_seconds=max_shift,
    )
