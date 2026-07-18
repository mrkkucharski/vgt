from pathlib import Path

import pytest

from vgt.chords import ChordDetectionError, _parse_chordino_csv, _snap_segments_to_grid, detect_chords

FIXTURE_SOURCE = Path(__file__).parents[1] / "test" / "Reaper Project" / "Media" / "Paris Metro Punk.mp3"


def test_snap_segments_to_grid_moves_boundaries_onto_the_nearest_beat() -> None:
    grid = [0.0, 0.5, 1.0, 1.5, 2.0]

    segments = _snap_segments_to_grid([(0.05, 0.94, "C:maj"), (0.94, 1.98, "G:maj")], grid)

    assert segments == [
        {"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C:maj"},
        {"start_seconds": 1.0, "end_seconds": 2.0, "chord": "G:maj"},
    ]


def test_snap_segments_to_grid_merges_adjacent_segments_that_collapse_together() -> None:
    grid = [0.0, 1.0, 2.0, 3.0]

    # Two distinct raw segments that both snap onto the same [0.0, 1.0) span.
    segments = _snap_segments_to_grid([(0.1, 0.4, "C:maj"), (0.4, 0.9, "C:maj"), (0.9, 2.9, "G:maj")], grid)

    assert segments == [
        {"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C:maj"},
        {"start_seconds": 1.0, "end_seconds": 3.0, "chord": "G:maj"},
    ]


def test_snap_segments_to_grid_drops_segments_that_collapse_to_zero_length() -> None:
    grid = [0.0, 1.0, 2.0]

    segments = _snap_segments_to_grid([(0.1, 0.2, "C:maj"), (1.0, 2.0, "G:maj")], grid)

    assert segments == [{"start_seconds": 1.0, "end_seconds": 2.0, "chord": "G:maj"}]


def test_parse_chordino_csv_pairs_consecutive_chord_change_events() -> None:
    csv_text = '"track.wav",0.0,"N"\n"track.wav",1.0,"C:maj"\n"track.wav",3.0,"G:maj"\n"track.wav",4.0,"N"\n'

    segments = _parse_chordino_csv(csv_text)

    assert segments == [(1.0, 3.0, "C:maj"), (3.0, 4.0, "G:maj")]


def test_parse_chordino_csv_returns_none_for_too_few_events() -> None:
    assert _parse_chordino_csv('"track.wav",0.0,"C:maj"\n') is None


def test_detect_chords_uses_chordino_when_madmom_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import vgt.chords as chords_module

    monkeypatch.setattr(chords_module, "_madmom_chords", lambda source: None)
    monkeypatch.setattr(
        chords_module,
        "_chordino_chords",
        lambda source: [(0.0, 1.0, "C:maj"), (1.0, 2.0, "G:maj")],
    )

    result = detect_chords(FIXTURE_SOURCE, beat_times=[0.0, 1.0, 2.0])

    assert result["backend"] == "chordino"
    assert result["vocabulary"] == "maj_min"
    assert result["segments"] == [
        {"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C:maj"},
        {"start_seconds": 1.0, "end_seconds": 2.0, "chord": "G:maj"},
    ]


def test_detect_chords_rejects_too_short_a_grid() -> None:
    with pytest.raises(ChordDetectionError, match="Too few beats"):
        detect_chords(FIXTURE_SOURCE, beat_times=[0.0])


def test_detect_chords_falls_back_to_template_classifier_snapped_to_the_given_grid() -> None:
    """Neither madmom nor sonic-annotator are installed in this environment,
    so this exercises the always-available chroma-template path end to end,
    against the real fixture, and asserts every boundary lands on the exact
    grid passed in (not some independently detected one)."""
    beat_times = [round(0.5 * i, 6) for i in range(200)]  # a dense synthetic grid

    result = detect_chords(FIXTURE_SOURCE, beat_times=beat_times)

    assert result["backend"] == "librosa"
    grid = set(result["beat_times"])
    assert grid == set(beat_times)
    for segment in result["segments"]:
        assert segment["start_seconds"] in grid
        assert segment["end_seconds"] in grid
