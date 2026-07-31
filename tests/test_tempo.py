import builtins
import sys
import types
from pathlib import Path

import pytest

from vgt.tempo import (
    TempoDetectionError,
    _MIN_SPAN_BEATS,
    _piecewise_spans,
    build_tempo_grid,
    detect_beats,
    infer_downbeat_from_chords,
)

FIXTURE_SOURCE = Path(__file__).parents[1] / "test" / "Reaper Project" / "Media" / "Paris Metro Punk.mp3"


def _constant_beats(bpm: float, count: int, offset: float = 0.1) -> list[float]:
    interval = 60.0 / bpm
    return [offset + i * interval for i in range(count)]


def test_build_tempo_grid_reports_constant_mode_for_steady_beats() -> None:
    beat_times = _constant_beats(120.0, 64)
    beat_positions = [(i % 4) + 1 for i in range(len(beat_times))]

    grid = build_tempo_grid(beat_times, beat_positions, backend="librosa")

    assert grid["mode"] == "constant"
    assert grid["spans"] is None
    assert grid["bpm"] == 120.0
    assert grid["time_signature"] == "4/4"
    assert grid["downbeat_detected"] is True
    assert grid["downbeat_offset_seconds"] == beat_times[0]
    assert grid["downbeat_source"] == "beat_tracker"
    assert grid["residual_seconds"] < 1e-6


def test_build_tempo_grid_reports_piecewise_mode_for_a_tempo_change() -> None:
    slow = _constant_beats(80.0, 32, offset=0.1)
    fast = _constant_beats(160.0, 32, offset=slow[-1] + 60.0 / 80.0)
    beat_times = slow + fast
    beat_positions = [1] * len(beat_times)  # librosa fallback: downbeats unknown

    grid = build_tempo_grid(beat_times, beat_positions, backend="librosa")

    assert grid["mode"] == "piecewise"
    assert grid["spans"]
    assert len(grid["spans"]) >= 2
    assert grid["spans"][0]["bpm"] == 80.0
    assert grid["spans"][-1]["bpm"] == 160.0
    # librosa fallback never sees downbeats, so time signature falls back to
    # the caller's hint (or 4/4 if none given).
    assert grid["time_signature"] == "4/4"
    assert grid["downbeat_detected"] is False
    assert grid["downbeat_offset_seconds"] is None
    assert grid["downbeat_source"] is None


def test_build_tempo_grid_uses_time_signature_hint_when_downbeats_unknown() -> None:
    beat_times = _constant_beats(100.0, 16)
    beat_positions = [1] * len(beat_times)

    grid = build_tempo_grid(beat_times, beat_positions, backend="librosa", settings={"time_signature_hint": "3/4"})

    assert grid["time_signature"] == "3/4"
    assert grid["downbeat_detected"] is False


def test_build_tempo_grid_reads_downbeats_when_available() -> None:
    beat_times = _constant_beats(120.0, 16)
    beat_positions = [(i % 3) + 1 for i in range(len(beat_times))]  # 3/4, madmom-style

    grid = build_tempo_grid(beat_times, beat_positions, backend="madmom")

    assert grid["time_signature"] == "3/4"
    assert grid["downbeat_detected"] is True
    assert grid["downbeat_offset_seconds"] == beat_times[0]
    assert grid["downbeat_source"] == "beat_tracker"


def test_piecewise_spans_never_split_below_the_minimum_span_size() -> None:
    """Rapidly oscillating tempo (a stress case, not realistic music) used to
    make the greedy splitter emit near-single-beat spans: closing a span
    rewinds `segment_start`, and the split loop must re-check the minimum
    span length from that new position rather than continuing to advance a
    bound computed before the rewind."""
    beat_times = []
    t = 0.0
    for i in range(120):
        bpm = 60 + (i % 6) * 40
        t += 60.0 / bpm
        beat_times.append(t)

    spans, _residual = _piecewise_spans(beat_times)

    for span in spans[:-1]:  # the trailing span may be shorter (leftover beats)
        tolerance = 1e-9
        beat_count = sum(
            1 for t in beat_times if span["start_seconds"] - tolerance <= t <= span["end_seconds"] + tolerance
        )
        assert beat_count >= _MIN_SPAN_BEATS


def _install_fake_madmom(monkeypatch: pytest.MonkeyPatch, *, raises: bool) -> None:
    """Simulate an importable-but-broken madmom, the failure mode its known
    fragility (see tempo.py's module docstring) actually produces: it imports
    but blows up once its models run against real audio/NumPy."""

    class _Processor:
        def __call__(self, *args: object, **kwargs: object) -> object:
            if raises:
                raise RuntimeError("madmom model processing failed")
            raise AssertionError("test double should always raise")

    downbeats_module = types.ModuleType("madmom.features.downbeats")
    downbeats_module.DBNDownBeatTrackingProcessor = _Processor  # type: ignore[attr-defined]
    downbeats_module.RNNDownBeatProcessor = _Processor  # type: ignore[attr-defined]
    features_module = types.ModuleType("madmom.features")
    features_module.downbeats = downbeats_module  # type: ignore[attr-defined]
    madmom_module = types.ModuleType("madmom")
    madmom_module.features = features_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "madmom", madmom_module)
    monkeypatch.setitem(sys.modules, "madmom.features", features_module)
    monkeypatch.setitem(sys.modules, "madmom.features.downbeats", downbeats_module)


def test_detect_beats_falls_back_to_librosa_when_madmom_processing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """madmom importing successfully doesn't mean it can process audio (its
    last release predates modern Python/NumPy) -- a runtime failure, not just
    an ImportError, must still fall back to librosa rather than propagate."""
    _install_fake_madmom(monkeypatch, raises=True)

    beat_times, beat_positions, backend = detect_beats(FIXTURE_SOURCE)

    assert backend == "librosa"
    assert len(beat_times) >= 2
    assert len(beat_positions) == len(beat_times)


def test_detect_beats_falls_back_when_madmom_initialization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incompatible madmom can raise while importing its submodule."""
    source = tmp_path / "source.mp3"
    source.touch()
    original_import = builtins.__import__

    def failing_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "madmom.features.downbeats":
            raise RuntimeError("madmom extension initialization failed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    monkeypatch.setattr("vgt.tempo._librosa_beats", lambda _source: ([0.0, 0.5], [1, 1]))

    assert detect_beats(source) == ([0.0, 0.5], [1, 1], "librosa")


def test_detect_beats_normalizes_a_terminal_fallback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp3"
    source.touch()
    monkeypatch.setattr("vgt.tempo._madmom_beats", lambda _source: None)

    def failing_librosa(_source: Path) -> tuple[list[float], list[int]]:
        raise RuntimeError("decoder unavailable")

    monkeypatch.setattr("vgt.tempo._librosa_beats", failing_librosa)

    with pytest.raises(TempoDetectionError, match="librosa fallback failed"):
        detect_beats(source)


def _segments(bars: list[int], beats_per_bar: int = 4) -> list[dict[str, object]]:
    """Chord segments whose start times fall on the given bar (beat-index / beats_per_bar) boundaries."""
    return [{"start_seconds": bar * beats_per_bar, "end_seconds": bar * beats_per_bar + 1, "chord": "C:maj"} for bar in bars]


def test_infer_downbeat_from_chords_reads_bar_lines_from_chord_changes() -> None:
    """Mirrors the Hotel California Cover table in issue #276: chord changes
    land on beat indices 0, 32, 64, 128, 160, 192, 224, 256, 320, 352, 384 --
    every one an exact multiple of 4 beats -- so phase 0 fits with zero error."""
    beat_times = [float(i) for i in range(400)]
    segments = _segments([0, 8, 16, 32, 40, 48, 56, 64, 80, 88, 96])

    result = infer_downbeat_from_chords(beat_times, segments, "4/4")

    assert result == {
        "downbeat_detected": True,
        "downbeat_offset_seconds": 0.0,
        "downbeat_source": "chords",
    }


def test_infer_downbeat_from_chords_anchors_on_the_winning_phase() -> None:
    """Chord changes offset by 2 beats from every bar line (phase 2 of 4) must
    anchor there, not at beat 0."""
    beat_times = [float(i) for i in range(200)]
    segments = _segments([bar for bar in range(1, 40)], beats_per_bar=1)
    # Shift every boundary by 2 beats so bar lines actually sit at phase 2.
    segments = [
        {"start_seconds": s["start_seconds"] * 4 + 2, "end_seconds": s["start_seconds"] * 4 + 3, "chord": "C:maj"}
        for s in segments
    ]

    result = infer_downbeat_from_chords(beat_times, segments, "4/4")

    assert result is not None
    assert result["downbeat_offset_seconds"] == 2.0
    assert result["downbeat_source"] == "chords"


def test_infer_downbeat_from_chords_returns_none_with_too_few_chord_changes() -> None:
    beat_times = [float(i) for i in range(20)]
    segments = _segments([0, 2, 4])  # below the minimum evidence count

    assert infer_downbeat_from_chords(beat_times, segments, "4/4") is None


def test_infer_downbeat_from_chords_returns_none_for_syncopated_harmonic_rhythm() -> None:
    """Chord changes evenly spread across every possible phase (no single one
    fits tightly, or clearly beats the others) must leave the downbeat
    undetected rather than guess."""
    beat_times = [float(i) for i in range(200)]
    # 12 consecutive beat indices cover each of the 4 possible phases equally
    # often, so every phase scores the same (poor) mean error.
    segments = _segments(list(range(12)), beats_per_bar=1)

    assert infer_downbeat_from_chords(beat_times, segments, "4/4") is None


def test_infer_downbeat_from_chords_returns_none_without_a_usable_time_signature() -> None:
    beat_times = [float(i) for i in range(200)]
    segments = _segments([0, 8, 16, 32, 40])

    assert infer_downbeat_from_chords(beat_times, segments, None) is None
    assert infer_downbeat_from_chords(beat_times, segments, "garbage") is None


def test_infer_downbeat_from_chords_returns_none_when_grid_shorter_than_bar() -> None:
    beat_times = [0.0, 1.0]  # fewer beats than the 4/4 bar needs
    segments = _segments([0])

    assert infer_downbeat_from_chords(beat_times, segments, "4/4") is None


def test_infer_downbeat_from_chords_ignores_segments_missing_start_seconds() -> None:
    """A malformed chord segment (e.g. from a hand-edited sidecar) must be
    skipped, not crash the whole analyze() run with a KeyError."""
    beat_times = [float(i) for i in range(400)]
    segments = _segments([0, 8, 16, 32, 40, 48, 56, 64, 80, 88, 96])
    segments.append({"end_seconds": 12.0, "chord": "C:maj"})  # no start_seconds

    result = infer_downbeat_from_chords(beat_times, segments, "4/4")

    assert result == {
        "downbeat_detected": True,
        "downbeat_offset_seconds": 0.0,
        "downbeat_source": "chords",
    }
