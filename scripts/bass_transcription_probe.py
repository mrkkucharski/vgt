"""Measure the quality of a monophonic (single-line) transcription.

Evaluation-only. Reads note-events CSV(s) and scores them against a pitch
reference derived from the source stem itself. It neither runs a transcription
backend nor writes into a vgt project.

    uv run python scripts/bass_transcription_probe.py NOTES.csv --stem bass.wav

This is the bass counterpart to `guitar_transcription_probe.py`, and it exists
because the guitar probe's headline metrics do not transfer to a single-line
instrument:

* **Chord agreement is meaningless here.** A bass plays one note; agreement with
  a detected chord symbol says nothing about whether the *right* note was found.
* **Harmonic-ghost share is the wrong shape.** For guitar it measures how much
  of a legitimately polyphonic texture is spurious. For bass, every simultaneous
  note is spurious, so the useful question is which single pitch was chosen.

So this probe scores against a frame-level pitch reference instead, and reports
**precision alongside recall**. That pairing is the whole point: a transcription
holding 22 simultaneous voices scores >90% recall by brute force (with that many
notes sounding, one of them is nearly always right) while being useless. Only
precision exposes that, because it counts every extra simultaneous pitch as a
false positive. Read the F column, never recall alone.

Two independent reference estimators are available, from different algorithm
families, so neither one's failure modes explain the other's:

* `pyin` (default) -- time-domain autocorrelation with a probabilistic voicing
  model, via `librosa.pyin`. Also reports which frames are voiced.
* `cqt` -- frequency-domain: a constant-Q magnitude spectrogram scored per
  semitone by a weighted sum of energy at the fundamental, octave, 12th and
  double octave.

**Scoring pyin-backed output against the `pyin` reference is circular** -- the
tracker is being compared to itself. Use `--reference cqt` for that, and use
`--agreement` to check the two references agree before trusting either.

Computing a reference costs ~40 s for a 3-minute stem, so pass `--cache FILE`
to reuse it across runs and across variants.

Onset scoring, and why it is a separate mode
--------------------------------------------
Everything above is *frame*-level: it asks which pitch was sounding at each
instant. That question is structurally blind to a re-articulation. Playing the
same fret four times running sounds the same pitch throughout, so a transcript
that emits one held note and one that emits four score **identically** -- the
frame metrics literally cannot tell them apart, and the tracker's maximal-run
segmentation was getting this wrong on every repeated note.

`--onset-reference NOTES.json` scores note *starts* instead, against a
hand-annotated note list, and is the only metric here that moves when
re-articulation splitting changes. It needs a real annotation because no
estimator derived from the stem can supply one: the estimators this probe
builds are frame-level too and share exactly the same blind spot.

    uv run python scripts/bass_transcription_probe.py NOTES.csv --stem bass.wav \
      --onset-reference tests/fixtures/bass_7rivers/hand_corrected_notes.json

Scoring is restricted to the annotation's window, so a partially annotated song
does not count every unannotated note as a false positive.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

Note = tuple[float, float, int, int]  # start_s, end_s, pitch, velocity

# Frames quieter than this percentile of stem RMS are treated as "the instrument
# is not playing here" and excluded from recall. Without it, the gaps between
# phrases dominate the missed column and every variant looks equally bad.
QUIET_PERCENTILE = 25
# A detected pitch within this many semitones of the reference counts as correct.
PITCH_TOLERANCE_SEMITONES = 0.5
# Reported separately from plain "wrong": an octave error means the tracker found
# the right note class but the wrong partial, which is a different fix than a
# genuinely mistaken pitch.
OCTAVE_INTERVALS = (12, 24)


def load_notes(path: Path) -> list[Note]:
    """Read a note-events CSV.

    Rows carry a variable-length trailing pitch-bend sequence, so only the first
    four columns are read and the rest ignored -- the same lenient contract
    `vgt.transcribe.parse_notes_csv` implements.
    """
    notes: list[Note] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise SystemExit(f"{path}: empty notes CSV")
        for row in reader:
            if len(row) < 4:
                continue
            notes.append((float(row[0]), float(row[1]), int(float(row[2])), int(float(row[3]))))
    notes.sort()
    return notes


def _load_audio(stem: Path, sample_rate: int):
    import librosa

    audio, rate = librosa.load(str(stem), sr=sample_rate, mono=True)
    return audio, int(rate)


def pyin_reference(stem: Path, *, sample_rate: int, hop: int, fmin: float, fmax: float):
    """`(midi_per_frame, voiced_mask, rms)` from librosa's pYIN."""
    import librosa
    import numpy as np

    audio, rate = _load_audio(stem, sample_rate)
    f0, voiced, _probability = librosa.pyin(
        audio, fmin=fmin, fmax=fmax, sr=rate, frame_length=2048, hop_length=hop
    )
    midi = np.full(len(f0), np.nan)
    usable = voiced & ~np.isnan(f0)
    midi[usable] = librosa.hz_to_midi(f0[usable])
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=hop)[0]
    return midi, voiced, rms


def cqt_reference(stem: Path, *, sample_rate: int, hop: int, fmin: float, fmax: float):
    """`(midi_per_frame, voiced_mask, rms)` from a CQT harmonic sum.

    Every frame is "voiced": an argmax always returns something, so this
    estimator carries no voicing decision of its own and relies entirely on the
    RMS gate to exclude silence.
    """
    import librosa
    import numpy as np

    audio, rate = _load_audio(stem, sample_rate)
    low_midi = max(0, int(round(librosa.hz_to_midi(fmin))) - 1)
    high_midi = int(round(librosa.hz_to_midi(fmax))) + 24  # headroom for the partials summed below
    bins = high_midi - low_midi
    spectrum = np.abs(
        librosa.cqt(audio, sr=rate, hop_length=hop, fmin=librosa.midi_to_hz(low_midi),
                    n_bins=bins, bins_per_octave=12)
    )
    salience = np.zeros_like(spectrum)
    for semitones, weight in ((0, 1.0), (12, 0.6), (19, 0.4), (24, 0.3)):
        shifted = np.zeros_like(spectrum)
        if semitones == 0:
            shifted = spectrum
        else:
            shifted[: bins - semitones] = spectrum[semitones:]
        salience += weight * shifted
    midi = (np.argmax(salience, axis=0) + low_midi).astype(float)
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=hop)[0]
    return midi, np.ones(len(midi), dtype=bool), rms


REFERENCES = {"pyin": pyin_reference, "cqt": cqt_reference}


def active_pitches(notes: list[Note], frame_count: int, sample_rate: int, hop: int) -> list[list[int]]:
    """Per-frame list of sounding pitches."""
    import math

    active: list[list[int]] = [[] for _ in range(frame_count)]
    for start_s, end_s, pitch, _velocity in notes:
        first = max(0, math.ceil(start_s * sample_rate / hop))
        last = min(frame_count, int(end_s * sample_rate / hop) + 1)
        for index in range(first, last):
            active[index].append(pitch)
    return active


def polyphony(notes: list[Note]) -> tuple[int, float]:
    """`(peak, median)` simultaneous voices.

    End events sort before starts at the same timestamp, so a note ending
    exactly where the next begins does not overlap it -- the convention
    `vgt.transcribe._note_comparison_metrics` uses.
    """
    edges = [(start, 1) for start, _e, _p, _v in notes] + [(end, -1) for _s, end, _p, _v in notes]
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    active = peak = 0
    weighted: list[tuple[float, int]] = []
    previous: float | None = None
    for time, delta in edges:
        if previous is not None and time > previous:
            weighted.append((time - previous, active))
        active += delta
        peak = max(peak, active)
        previous = time
    total = sum(span for span, _count in weighted)
    if not total:
        return peak, 0.0
    ordered = sorted(weighted, key=lambda item: item[1])
    seen = 0.0
    for span, count in ordered:
        seen += span
        if seen >= total / 2:
            return peak, float(count)
    return peak, 0.0


def score(notes: list[Note], reference, voiced, rms, *, sample_rate: int, hop: int) -> dict[str, float]:
    import numpy as np

    frame_count = min(len(reference), len(rms))
    reference, voiced, rms = reference[:frame_count], voiced[:frame_count], rms[:frame_count]
    loud = rms > np.percentile(rms, QUIET_PERCENTILE)
    judged = loud & voiced & ~np.isnan(reference)
    active = active_pitches(notes, frame_count, sample_rate, hop)

    correct = octave = wrong = missed = false_positives = 0
    for index in range(frame_count):
        sounding = active[index]
        if not judged[index]:
            # Anything sounding where the instrument is silent is spurious.
            false_positives += len(sounding)
            continue
        deltas = [pitch - reference[index] for pitch in sounding]
        hits = [delta for delta in deltas if abs(delta) <= PITCH_TOLERANCE_SEMITONES]
        false_positives += len(deltas) - len(hits)
        if not sounding:
            missed += 1
        elif hits:
            correct += 1
        elif any(abs(abs(delta) - interval) <= PITCH_TOLERANCE_SEMITONES for delta in deltas for interval in OCTAVE_INTERVALS):
            octave += 1
        else:
            wrong += 1

    graded = int(judged.sum()) or 1
    precision = correct / (correct + false_positives) if correct + false_positives else 0.0
    recall = correct / graded
    f_measure = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    durations = [end - start for start, end, _p, _v in notes] or [0.0]
    pitches = [pitch for _s, _e, pitch, _v in notes]
    peak_poly, median_poly = polyphony(notes)
    return {
        "notes": len(notes),
        "med_ms": statistics.median(durations) * 1000,
        "max_s": max(durations),
        "maxpoly": peak_poly,
        "medpoly": median_poly,
        "lo": min(pitches) if pitches else 0,
        "hi": max(pitches) if pitches else 0,
        "hit": 100 * correct / graded,
        "oct": 100 * octave / graded,
        "wrong": 100 * wrong / graded,
        "miss": 100 * missed / graded,
        "prec": 100 * precision,
        "rec": 100 * recall,
        "f": 100 * f_measure,
    }


# A detected onset this close to an annotated one counts as the same note. One
# pYIN frame is ~11.6 ms and the annotation was placed by ear against a
# waveform, so anything tighter would measure the annotator's mouse.
ONSET_TOLERANCE_S = 0.05


def load_onset_reference(path: Path) -> tuple[list[float], tuple[float, float]]:
    """Read a hand-annotated note list, returning `(onsets, window)`.

    The window is the span the human actually annotated -- see the fixture's
    README on why it is usually a prefix of the song rather than all of it.
    Times are relative to the same origin the scored CSV uses (the stem start),
    which `stem_offset_s` restores.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    offset = float(data.get("stem_offset_s", 0.0))
    onsets = sorted(offset + float(note[0]) for note in data["notes"])
    low, high = (offset + float(bound) for bound in data["window_s"])
    return onsets, (low, high)


def score_onsets(notes: list[Note], reference: list[float], window: tuple[float, float]) -> dict[str, float]:
    """Match detected note starts to annotated ones, one to one, nearest first.

    One-to-one matching is the point: without it, a transcript that shattered
    one played note into six fragments would score six matches for it and read
    as *better* than a correct one. Each annotated onset consumes at most one
    detection, and every unconsumed detection is a false positive -- so
    over-splitting shows up as falling precision, exactly as over-detection
    does in the frame metrics above.
    """
    low, high = window
    detected = sorted(start for start, _end, _pitch, _velocity in notes if low - ONSET_TOLERANCE_S <= start <= high + ONSET_TOLERANCE_S)
    graded = [onset for onset in reference if low - 1e-6 <= onset <= high + 1e-6]
    taken = [False] * len(detected)
    matched = 0
    for onset in graded:
        best, best_distance = None, ONSET_TOLERANCE_S
        for index, start in enumerate(detected):
            if taken[index]:
                continue
            if abs(start - onset) <= best_distance:
                best, best_distance = index, abs(start - onset)
        if best is not None:
            taken[best] = True
            matched += 1
    precision = matched / len(detected) if detected else 0.0
    recall = matched / len(graded) if graded else 0.0
    return {
        "det": len(detected),
        "ref": len(graded),
        "match": matched,
        "oprec": 100 * precision,
        "orec": 100 * recall,
        "of": 100 * (2 * precision * recall / (precision + recall) if precision + recall else 0.0),
    }


ONSET_COLUMNS = (
    ("det", "{:>5.0f}"), ("ref", "{:>5.0f}"), ("match", "{:>6.0f}"),
    ("oprec", "{:>6.1f}"), ("orec", "{:>6.1f}"), ("of", "{:>6.1f}"),
)


COLUMNS = (
    ("notes", "{:>5.0f}"), ("med_ms", "{:>7.0f}"), ("max_s", "{:>7.1f}"),
    ("maxpoly", "{:>8.0f}"), ("medpoly", "{:>8.0f}"), ("lo", "{:>4.0f}"), ("hi", "{:>4.0f}"),
    ("hit", "{:>6.1f}"), ("oct", "{:>6.1f}"), ("wrong", "{:>6.1f}"), ("miss", "{:>6.1f}"),
    ("prec", "{:>6.1f}"), ("rec", "{:>6.1f}"), ("f", "{:>6.1f}"),
)


def label_for(path: Path, paths: list[Path]) -> str:
    """Use the containing directory when several inputs share a filename, so a
    sweep laid out as `sweep/<variant>/bass_basic_pitch.csv` reads directly --
    the same convention `guitar_transcription_probe.py` uses."""
    if sum(other.name == path.name for other in paths) > 1:
        return path.parent.name
    return path.stem


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("notes", nargs="+", type=Path, help="note-events CSV(s) to score")
    parser.add_argument("--stem", required=True, type=Path, help="the source audio the CSVs were transcribed from")
    parser.add_argument("--reference", choices=sorted(REFERENCES), default="pyin",
                        help="reference estimator (default: pyin; use cqt to score pyin-backed output)")
    parser.add_argument("--fmin", type=float, default=35.0, help="reference search floor in Hz (default: 35)")
    parser.add_argument("--fmax", type=float, default=330.0, help="reference search ceiling in Hz (default: 330)")
    parser.add_argument("--sample-rate", type=int, default=22050)
    parser.add_argument("--hop", type=int, default=256)
    parser.add_argument("--cache", type=Path, help="npz file to store/reuse the computed reference")
    parser.add_argument("--agreement", action="store_true",
                        help="also report how often the two reference estimators agree")
    parser.add_argument("--onset-reference", type=Path,
                        help="hand-annotated note list (JSON) to additionally score note starts against")
    args = parser.parse_args()

    import numpy as np

    def build(name: str):
        return REFERENCES[name](args.stem, sample_rate=args.sample_rate, hop=args.hop, fmin=args.fmin, fmax=args.fmax)

    cache_key = f"{args.reference}_"
    if args.cache and args.cache.is_file():
        stored = np.load(args.cache)
        if f"{cache_key}midi" in stored:
            reference, voiced, rms = stored[f"{cache_key}midi"], stored[f"{cache_key}voiced"], stored[f"{cache_key}rms"]
        else:
            reference, voiced, rms = build(args.reference)
            np.savez(args.cache, **{k: stored[k] for k in stored.files},
                     **{f"{cache_key}midi": reference, f"{cache_key}voiced": voiced, f"{cache_key}rms": rms})
    else:
        reference, voiced, rms = build(args.reference)
        if args.cache:
            np.savez(args.cache, **{f"{cache_key}midi": reference, f"{cache_key}voiced": voiced, f"{cache_key}rms": rms})

    print(f"stem      {args.stem}")
    print(f"reference {args.reference}  ({args.fmin:g}-{args.fmax:g} Hz, hop {args.hop} @ {args.sample_rate} Hz)")
    # Over exactly the frames scoring grades, not every frame: an argmax-based
    # estimator returns a pitch for silence too, and including those would widen
    # the reported range well past what the instrument actually plays.
    count = min(len(reference), len(rms))
    graded_mask = (
        (rms[:count] > np.percentile(rms[:count], QUIET_PERCENTILE))
        & voiced[:count]
        & ~np.isnan(reference[:count])
    )
    graded = reference[:count][graded_mask]
    if len(graded):
        print(
            f"reference pitch  p1 {np.percentile(graded, 1):.1f}  median {np.median(graded):.1f} "
            f" p99 {np.percentile(graded, 99):.1f} (MIDI, over graded frames only)"
        )
    if args.reference == "pyin":
        print(f"voiced    {100 * float(np.mean(voiced)):.1f}% of frames")

    if args.agreement:
        other_name = "cqt" if args.reference == "pyin" else "pyin"
        other, other_voiced, _rms = build(other_name)
        count = min(len(reference), len(other), len(rms))
        loud = rms[:count] > np.percentile(rms[:count], QUIET_PERCENTILE)
        comparable = loud & ~np.isnan(reference[:count]) & ~np.isnan(other[:count]) & voiced[:count] & other_voiced[:count]
        agree = comparable & (np.abs(np.round(reference[:count]) - np.round(other[:count])) <= PITCH_TOLERANCE_SEMITONES)
        total = int(comparable.sum()) or 1
        print(f"agreement {100 * int(agree.sum()) / total:.1f}% with the {other_name} reference on comparable loud frames")

    header = f"{'variant':<28}" + "".join(f"{name:>{len(fmt.format(0))}}" for name, fmt in COLUMNS)
    print()
    print(header)
    print("-" * len(header))
    for path in args.notes:
        row = score(load_notes(path), reference, voiced, rms, sample_rate=args.sample_rate, hop=args.hop)
        print(f"{label_for(path, args.notes):<28}" + "".join(fmt.format(row[name]) for name, fmt in COLUMNS))
    print()
    print("hit/oct/wrong/miss are shares of graded frames; prec/rec/f count every extra")
    print("simultaneous pitch as a false positive. Read f, not rec.")

    if args.onset_reference:
        onsets, window = load_onset_reference(args.onset_reference)
        print()
        print(f"onset reference {args.onset_reference}")
        print(f"annotated window {window[0]:.3f}-{window[1]:.3f} s ({len(onsets)} notes); "
              f"matching tolerance {1000 * ONSET_TOLERANCE_S:.0f} ms")
        header = f"{'variant':<28}" + "".join(f"{name:>{len(fmt.format(0))}}" for name, fmt in ONSET_COLUMNS)
        print()
        print(header)
        print("-" * len(header))
        for path in args.notes:
            row = score_onsets(load_notes(path), onsets, window)
            print(f"{label_for(path, args.notes):<28}" + "".join(fmt.format(row[name]) for name, fmt in ONSET_COLUMNS))
        print()
        print("det/ref/match are note counts inside the annotated window; oprec/orec/of are")
        print("onset precision/recall/F. This is the only table re-articulation splitting moves.")


if __name__ == "__main__":
    main()
