"""Offline scoring of a candidate drum MIDI/events file against ground truth.

Built for issue #182: measurement scaffolding for the drums transcription
cleanup work (see ``docs/drums-transcription-timing-findings.md``). Reuses
the one-to-one onset matching and precision/recall/F1 conventions in
``vgt.drum_evaluation`` rather than inventing a new scoring format. Nothing
here invokes REAPER, the network, or a transcription model -- both inputs
are local files (Standard MIDI Format or DrumScript event JSON).

The bundled SMF reader only understands what DAW-exported drum MIDI needs:
note on/off, tempo meta events, and generic meta/sysex skipping. It does not
attempt full MIDI-spec coverage (e.g. system common/realtime bytes).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

from vgt.drum_evaluation import ONSET_TOLERANCE_SECONDS, evaluate_instruments, instrument_onsets, read_event_json


DEFAULT_WINDOW_SECONDS = 2.0

# General MIDI percussion key map (channel 10), the de facto standard for
# single-track drum MIDI exported by DAWs and DrumScript alike.
GM_DRUM_MAP: dict[int, str] = {
    35: "kick", 36: "kick",
    37: "snare", 38: "snare", 40: "snare",
    42: "hihat_closed", 44: "hihat_pedal", 46: "hihat_open",
    41: "tom_low", 43: "tom_low",
    45: "tom_mid", 47: "tom_mid",
    48: "tom_high", 50: "tom_high",
    49: "crash", 57: "crash",
    51: "ride", 59: "ride",
    39: "clap",
}


@dataclass(frozen=True)
class MidiNote:
    time_sec: float
    note: int
    velocity: int


def _read_var_len(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return value, offset


def _tick_to_seconds(tick: int, tempo_changes: list[tuple[int, int]], ticks_per_quarter: int) -> float:
    seconds = 0.0
    prev_tick = 0
    prev_tempo = tempo_changes[0][1]
    for change_tick, tempo in tempo_changes:
        if change_tick > tick:
            break
        seconds += (change_tick - prev_tick) * prev_tempo / ticks_per_quarter / 1_000_000
        prev_tick = change_tick
        prev_tempo = tempo
    seconds += (tick - prev_tick) * prev_tempo / ticks_per_quarter / 1_000_000
    return seconds


def read_midi_notes(path: Path) -> list[MidiNote]:
    """Parse note-on events (with velocity > 0) out of a Standard MIDI file."""
    data = path.read_bytes()
    if data[:4] != b"MThd":
        raise ValueError(f"{path}: not a standard MIDI file (missing MThd header)")
    header_len = struct.unpack(">I", data[4:8])[0]
    _format, num_tracks, division = struct.unpack(">HHH", data[8:8 + header_len])
    if division & 0x8000:
        raise ValueError(f"{path}: SMPTE-based MIDI division is not supported")
    ticks_per_quarter = division

    offset = 8 + header_len
    raw_events: list[tuple[int, int, int]] = []  # (tick, note, velocity); velocity 0 == note off
    tempo_changes: list[tuple[int, int]] = [(0, 500_000)]  # default 120 BPM

    for _ in range(num_tracks):
        if data[offset:offset + 4] != b"MTrk":
            raise ValueError(f"{path}: expected MTrk chunk")
        track_len = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        track_end = offset + 8 + track_len
        pos = offset + 8
        tick = 0
        running_status: int | None = None
        while pos < track_end:
            delta, pos = _read_var_len(data, pos)
            tick += delta
            status = data[pos]
            if status & 0x80:
                running_status = status
                pos += 1
            else:
                status = running_status
                if status is None:
                    raise ValueError(f"{path}: missing running status")
            if status == 0xFF:
                meta_type = data[pos]
                pos += 1
                length, pos = _read_var_len(data, pos)
                payload = data[pos:pos + length]
                pos += length
                if meta_type == 0x51 and length == 3:
                    tempo = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                    tempo_changes.append((tick, tempo))
            elif status in (0xF0, 0xF7):
                length, pos = _read_var_len(data, pos)
                pos += length
            else:
                event_type = status & 0xF0
                if event_type in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    note, velocity = data[pos], data[pos + 1]
                    pos += 2
                    if event_type == 0x90:
                        raw_events.append((tick, note, velocity))
                    elif event_type == 0x80:
                        raw_events.append((tick, note, 0))
                elif event_type in (0xC0, 0xD0):
                    pos += 1
                else:
                    raise ValueError(f"{path}: unsupported MIDI status byte 0x{status:02X}")
        offset = track_end

    tempo_changes.sort(key=lambda item: item[0])
    notes = [
        MidiNote(_tick_to_seconds(tick, tempo_changes, ticks_per_quarter), note, velocity)
        for tick, note, velocity in raw_events
        if velocity > 0
    ]
    return sorted(notes, key=lambda note: note.time_sec)


def midi_instrument_onsets(notes: list[MidiNote]) -> dict[str, list[float]]:
    """Map GM percussion note numbers to instrument labels; unmapped notes keep their number."""
    result: dict[str, list[float]] = {}
    for note in notes:
        label = GM_DRUM_MAP.get(note.note, f"note_{note.note}")
        result.setdefault(label, []).append(note.time_sec)
    return result


def read_onsets_from_path(path: Path) -> dict[str, list[float]]:
    """Load per-instrument onsets from either a .mid/.midi file or DrumScript event JSON."""
    suffix = path.suffix.lower()
    if suffix in (".mid", ".midi"):
        return midi_instrument_onsets(read_midi_notes(path))
    if suffix == ".json":
        return instrument_onsets(read_event_json(path))
    raise ValueError(f"{path}: unsupported input format (expected .mid, .midi, or .json)")


def _match_pairs(refs: list[float], preds: list[float], tolerance: float) -> list[tuple[float, float]]:
    """One-to-one match (max matches, then min total error); mirrors drum_evaluation._metrics.

    Returns matched (ground_truth_time, candidate_time) pairs so timing error
    can be computed, since the shared counts-only helper does not expose them.
    """
    refs = sorted(refs)
    preds = sorted(preds)
    score: list[list[tuple[int, float]]] = [[(0, 0.0) for _ in range(len(preds) + 1)] for _ in range(len(refs) + 1)]
    for ref_index in range(len(refs) - 1, -1, -1):
        for pred_index in range(len(preds) - 1, -1, -1):
            candidates = [score[ref_index + 1][pred_index], score[ref_index][pred_index + 1]]
            distance = abs(refs[ref_index] - preds[pred_index])
            if distance <= tolerance:
                matched, error = score[ref_index + 1][pred_index + 1]
                candidates.append((matched + 1, error + distance))
            score[ref_index][pred_index] = min(candidates, key=lambda item: (-item[0], item[1]))

    pairs: list[tuple[float, float]] = []
    ref_index = pred_index = 0
    while ref_index < len(refs) and pred_index < len(preds):
        distance = abs(refs[ref_index] - preds[pred_index])
        matched_here = (
            distance <= tolerance
            and score[ref_index][pred_index] == (
                score[ref_index + 1][pred_index + 1][0] + 1,
                score[ref_index + 1][pred_index + 1][1] + distance,
            )
        )
        if matched_here:
            pairs.append((refs[ref_index], preds[pred_index]))
            ref_index += 1
            pred_index += 1
        elif score[ref_index][pred_index] == score[ref_index + 1][pred_index]:
            ref_index += 1
        else:
            pred_index += 1
    return pairs


def _timing_stats(signed_errors_sec: list[float]) -> dict[str, object]:
    if not signed_errors_sec:
        return {"count": 0, "median_signed_error_ms": None, "mean_signed_error_ms": None}
    ordered = sorted(signed_errors_sec)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    mean = sum(ordered) / n
    return {"count": n, "median_signed_error_ms": median * 1000, "mean_signed_error_ms": mean * 1000}


def windowed_note_counts(times: list[float], window_seconds: float, total_duration: float) -> list[int]:
    """Bucket onset times into fixed-size windows (e.g. one bar, or a flat span)."""
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    num_windows = int(total_duration // window_seconds) + 1
    counts = [0] * num_windows
    for time in times:
        index = min(int(time // window_seconds), num_windows - 1)
        counts[index] += 1
    return counts


def score_onsets(
    ground_truth: dict[str, list[float]],
    candidate: dict[str, list[float]],
    *,
    tolerance: float = ONSET_TOLERANCE_SECONDS,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> dict[str, object]:
    """Score precision/recall/F1, matched-note timing error, and windowed note-count ratios."""
    metrics = evaluate_instruments(ground_truth, candidate, tolerance=tolerance)

    all_pairs: list[tuple[float, float]] = []
    per_instrument_timing: dict[str, object] = {}
    for instrument in sorted(set(ground_truth) | set(candidate)):
        pairs = _match_pairs(ground_truth.get(instrument, []), candidate.get(instrument, []), tolerance)
        all_pairs.extend(pairs)
        per_instrument_timing[instrument] = _timing_stats([pred - ref for ref, pred in pairs])
    global_timing = _timing_stats([pred - ref for ref, pred in all_pairs])

    gt_times = sorted(time for onsets in ground_truth.values() for time in onsets)
    cand_times = sorted(time for onsets in candidate.values() for time in onsets)
    duration = max(gt_times[-1] if gt_times else 0.0, cand_times[-1] if cand_times else 0.0)
    gt_counts = windowed_note_counts(gt_times, window_seconds, duration)
    cand_counts = windowed_note_counts(cand_times, window_seconds, duration)
    ratios: list[float | None] = []
    for gt_count, cand_count in zip(gt_counts, cand_counts):
        if gt_count == 0:
            ratios.append(1.0 if cand_count == 0 else None)
        else:
            ratios.append(cand_count / gt_count)

    return {
        "metrics": metrics,
        "timing": {"global": global_timing, "per_instrument": per_instrument_timing},
        "note_count_windows": {
            "window_seconds": window_seconds,
            "ground_truth_counts": gt_counts,
            "candidate_counts": cand_counts,
            "ratios": ratios,
        },
    }
