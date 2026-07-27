from __future__ import annotations

import json
from pathlib import Path
import runpy

from vgt.drum_midi_score import (
    GM_DRUM_MAP,
    midi_instrument_onsets,
    read_midi_notes,
    read_onsets_from_path,
    score_onsets,
    windowed_note_counts,
)


# 1000 ticks/quarter at 60 BPM (1_000_000 us/quarter) makes 1 tick == 1 ms,
# so fixtures can shift onsets by an exact millisecond count.
TICKS_PER_QUARTER = 1000
NOTE_DURATION_TICKS = 10


def _var_len(value: int) -> bytes:
    chunks = [value & 0x7F]
    value >>= 7
    while value:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(chunks))


def _build_midi(notes: list[tuple[int, int]]) -> bytes:
    """notes: list of (tick, midi_note_number); each gets a fixed-length on/off pair."""
    events: list[tuple[int, bytes]] = []
    for tick, note in notes:
        events.append((tick, bytes([0x99, note, 100])))
        events.append((tick + NOTE_DURATION_TICKS, bytes([0x89, note, 0])))
    events.sort(key=lambda item: item[0])

    track_data = bytearray()
    track_data += _var_len(0) + bytes([0xFF, 0x51, 0x03]) + (1_000_000).to_bytes(3, "big")
    prev_tick = 0
    for tick, data in events:
        track_data += _var_len(tick - prev_tick) + data
        prev_tick = tick
    track_data += _var_len(0) + bytes([0xFF, 0x2F, 0x00])

    header = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") + TICKS_PER_QUARTER.to_bytes(2, "big")
    return header + b"MTrk" + len(track_data).to_bytes(4, "big") + bytes(track_data)


def _write_midi(path: Path, notes: list[tuple[int, int]]) -> Path:
    path.write_bytes(_build_midi(notes))
    return path


def test_read_midi_notes_converts_ticks_to_seconds_via_tempo_meta(tmp_path: Path) -> None:
    path = _write_midi(tmp_path / "notes.mid", [(0, 36), (1000, 38)])
    notes = read_midi_notes(path)
    assert [round(note.time_sec, 3) for note in notes] == [0.0, 1.0]
    assert [note.note for note in notes] == [36, 38]


def test_midi_instrument_onsets_maps_gm_percussion_notes(tmp_path: Path) -> None:
    path = _write_midi(tmp_path / "notes.mid", [(0, 36), (500, 42)])
    onsets = midi_instrument_onsets(read_midi_notes(path))
    assert onsets == {"kick": [0.0], GM_DRUM_MAP[42]: [0.5]}


def test_perfect_match_scores_f1_one_with_zero_timing_error(tmp_path: Path) -> None:
    ground_truth = _write_midi(tmp_path / "gt.mid", [(0, 36), (500, 38), (1000, 36)])
    candidate = _write_midi(tmp_path / "cand.mid", [(0, 36), (500, 38), (1000, 36)])
    report = score_onsets(read_onsets_from_path(ground_truth), read_onsets_from_path(candidate))
    assert report["metrics"]["global"]["f1"] == 1.0
    assert report["timing"]["global"] == {"count": 3, "median_signed_error_ms": 0.0, "mean_signed_error_ms": 0.0}


def test_shifted_candidate_reports_systematic_signed_offset(tmp_path: Path) -> None:
    ground_truth = _write_midi(tmp_path / "gt.mid", [(0, 36), (500, 38), (1000, 36)])
    # Every onset arrives 45ms late, matching the documented DrumScript offset.
    candidate = _write_midi(tmp_path / "cand.mid", [(45, 36), (545, 38), (1045, 36)])
    report = score_onsets(read_onsets_from_path(ground_truth), read_onsets_from_path(candidate))
    assert report["metrics"]["global"]["f1"] == 1.0
    assert round(report["timing"]["global"]["median_signed_error_ms"], 6) == 45.0
    assert round(report["timing"]["global"]["mean_signed_error_ms"], 6) == 45.0


def test_missing_notes_reduce_recall_not_precision(tmp_path: Path) -> None:
    ground_truth = _write_midi(tmp_path / "gt.mid", [(0, 36), (500, 38), (1000, 36), (1500, 38)])
    candidate = _write_midi(tmp_path / "cand.mid", [(0, 36), (1000, 36)])
    report = score_onsets(read_onsets_from_path(ground_truth), read_onsets_from_path(candidate))
    assert report["metrics"]["global"] == {
        "true_positives": 2, "false_positives": 0, "false_negatives": 2,
        "precision": 1.0, "recall": 0.5, "f1": 2 / 3,
    }


def test_extra_notes_reduce_precision_not_recall(tmp_path: Path) -> None:
    ground_truth = _write_midi(tmp_path / "gt.mid", [(0, 36), (1000, 36)])
    candidate = _write_midi(tmp_path / "cand.mid", [(0, 36), (500, 42), (1000, 36), (1500, 42)])
    report = score_onsets(read_onsets_from_path(ground_truth), read_onsets_from_path(candidate))
    assert report["metrics"]["global"] == {
        "true_positives": 2, "false_positives": 2, "false_negatives": 0,
        "precision": 0.5, "recall": 1.0, "f1": 2 / 3,
    }


def test_windowed_note_counts_bucket_by_fixed_window() -> None:
    assert windowed_note_counts([0.1, 0.9, 1.1, 3.9], window_seconds=1.0, total_duration=3.9) == [2, 1, 0, 1]


def test_note_count_ratio_flags_over_and_under_detection(tmp_path: Path) -> None:
    # Bar 0: ground truth has 2 notes, candidate has 4 (over-detection).
    # Bar 1: ground truth has 2 notes, candidate has 0 (missing section).
    ground_truth = _write_midi(tmp_path / "gt.mid", [(0, 36), (500, 38), (1000, 36), (1500, 38)])
    candidate = _write_midi(tmp_path / "cand.mid", [(0, 36), (100, 38), (500, 36), (900, 38)])
    report = score_onsets(
        read_onsets_from_path(ground_truth), read_onsets_from_path(candidate), window_seconds=1.0,
    )
    windows = report["note_count_windows"]
    assert windows["ground_truth_counts"] == [2, 2]
    assert windows["candidate_counts"] == [4, 0]
    assert windows["ratios"] == [2.0, 0.0]


def test_cli_scores_checked_in_json_events_fixture() -> None:
    root = Path(__file__).parent / "fixtures" / "drum_evaluation"
    main = runpy.run_path(str(Path("scripts/drum_midi_score.py")))["main"]
    events_path = root / "events" / "pattern-a.json"
    assert main([str(events_path), str(events_path)]) == 0


def test_cli_writes_deterministic_json_report(tmp_path: Path) -> None:
    ground_truth = _write_midi(tmp_path / "gt.mid", [(0, 36), (500, 38)])
    candidate = _write_midi(tmp_path / "cand.mid", [(20, 36), (520, 38)])
    output = tmp_path / "report.json"
    main = runpy.run_path(str(Path("scripts/drum_midi_score.py")))["main"]
    assert main([str(candidate), str(ground_truth), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["kind"] == "offline-drum-midi-score"
    assert report["metrics"]["global"]["f1"] == 1.0
    assert round(report["timing"]["global"]["median_signed_error_ms"], 6) == 20.0
