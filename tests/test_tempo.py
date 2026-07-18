from vgt.tempo import build_tempo_grid


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
    assert grid["downbeat_offset_seconds"] == beat_times[0]
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


def test_build_tempo_grid_uses_time_signature_hint_when_downbeats_unknown() -> None:
    beat_times = _constant_beats(100.0, 16)
    beat_positions = [1] * len(beat_times)

    grid = build_tempo_grid(beat_times, beat_positions, backend="librosa", settings={"time_signature_hint": "3/4"})

    assert grid["time_signature"] == "3/4"


def test_build_tempo_grid_reads_downbeats_when_available() -> None:
    beat_times = _constant_beats(120.0, 16)
    beat_positions = [(i % 3) + 1 for i in range(len(beat_times))]  # 3/4, madmom-style

    grid = build_tempo_grid(beat_times, beat_positions, backend="madmom")

    assert grid["time_signature"] == "3/4"
    assert grid["downbeat_offset_seconds"] == beat_times[0]
