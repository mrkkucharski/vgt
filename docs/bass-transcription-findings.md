# Bass transcription findings

> Part of the per-instrument transcription evidence indexed in
> [instrument-transcription-findings.md](instrument-transcription-findings.md),
> which also carries the shared measurement method and the lessons that
> generalize across instruments.

Status: **implemented 2026-07-29/30.** `bass` no longer uses Basic Pitch at all.
`src/vgt/pyin_notes.py` adds a `pyin` backend — a monophonic F0 tracker — and
`bass` resolves to it by default, with a three-stage cleanup pipeline (merge →
deblip → clamp). The two Basic Pitch profiles are retained under explicit names
(`bass-basic-pitch`, `bass-monophonic`) for comparison only. This changes
`bass`'s `settings_hash`, so an existing bass transcription correctly invalidates
and re-transcribes once on the next run — no migration needed.
`bass-basic-pitch`'s hash is deliberately byte-identical to the old `bass` hash,
and no other target's hash or output changes.

Investigated 2026-07-29 after a user report that a real project's bass MIDI was
"completely wrong — so many notes at a time (polyphonic) does not match bass
guitar."

Read in order: "The complaint" establishes the failure, "Root cause" explains it,
"Parameter sweep" shows that **no Basic Pitch setting fixes it** (this is the
section that distinguishes bass from guitar — for guitar, retuning worked), and
"The replacement" covers what shipped.

The subject is the same track the guitar findings use: `7Rivers`, 178.6 s at
120.004 BPM 4/4, transcribed from the LALAL `bass` stem separated from the
original mix.

## The complaint, quantified

Shipping defaults for `bass` were `onset 0.5`, `frame 0.3`, `min-note 60 ms`,
melodia on, 30–400 Hz, and — unlike `guitar-acoustic` — **no cleanup pipeline at
all** (`cleanup: []`).

| Symptom | Measurement |
| --- | --- |
| Impossible polyphony | **22 simultaneous voices**, median **18**, with ≥17 voices sounding for **98%** of the song |
| Sustain runaway | 161 notes longer than 4 s, 73 longer than 10 s; the longest runs **119.6 s** |
| Where the note-time goes | **83%** of all note-seconds come from notes longer than 4 s |
| Out-of-range notes | 524 of 966 notes sit above the range the instrument actually plays; 140 above MIDI 48 entirely |
| Frame precision | **3.8%** — see below for why this is the number that matters |

A bass plays one note at a time. A median of 18 is not a tuning artifact, it is a
different instrument. Concretely, pitches 24, 26, 54, 55, 56, 58, 61, 63 and 66
were each held for 45–120 s at velocity 78–95: a permanent chord underneath the
whole track.

**Why precision, and why recall is a trap.** The obvious metric — "is a correct
pitch sounding right now?" — scores this output at **90.8%**, because with 18
voices sounding, one of them is nearly always right. That number is worthless.
Counting every *extra* simultaneous pitch as a false positive gives 3.8%
precision and an F-measure of **7.2%**. Every table below reports both; read `f`,
never `rec`.

## What the stem actually plays

Establishing ground truth without a hand-annotated reference: two estimators from
different algorithm families, so neither one's failure modes explain the other's.

- **pYIN** — time-domain autocorrelation with a probabilistic voicing model
  (`librosa.pyin`).
- **CQT harmonic sum** — frequency-domain: constant-Q magnitude spectrogram,
  scored per semitone by a weighted sum of energy at the fundamental, octave,
  12th and double octave.

Over graded frames (stem RMS above the 25th percentile):

| Estimator | p1 | median | p99 |
| --- | ---: | ---: | ---: |
| pYIN | 29.0 | **34.1** | 42.1 |
| CQT harmonic sum | 29.0 | **34.0** | 44.0 |

They agree on **85.9%** of comparable loud frames, and pYIN finds the stem voiced
87.6% of the time. So the real line is MIDI ~29–43, median 34 (B♭1) — squarely a
bass. Basic Pitch's 23–66 output spans B0 to F♯4.

This cross-check is what makes the rest of the document trustworthy, and
`--agreement` in the probe re-runs it on demand.

## Root cause

The same two defaults that break a strummed acoustic guitar (see
`guitar-transcription-findings.md`) break a bass stem far worse:

- **`DEFAULT_FRAME_THRESHOLD = 0.3`** — a bass stem is almost *entirely*
  sustained low-frequency energy, so frame activations essentially never fall
  below 0.3 and notes are never released.
- **`DEFAULT_MELODIA_TRICK = True`** — melodia then bridges the surviving gaps
  into multi-minute notes.

Two bass-specific factors make it worse than the guitar case:

- **The model is polyphonic and piano-trained.** Nothing in it prefers a single
  line, so a bass note's strong octave and 12th partials are detected as notes in
  their own right and there is no "one voice" prior to suppress them.
- **The frequency window admits the partials.** A 30–400 Hz band covers a bass
  fundamental (~30–100 Hz) *and* its first two partials, so ghosts land inside
  the band rather than above it.

## Parameter sweep

Eleven inferences over the same stem, `basic-pitch[onnx]==0.4.0`, ONNX
serialization, `--midi-tempo 120.004`. Scored against the **CQT** reference
(scoring against pYIN would be circular for the pyin row).

### Raw detections, before any cleanup

| variant | notes | med_ms | max_s | maxpoly | medpoly | range | prec | rec | **F** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| default-bass (shipping) | 966 | 506 | 119.6 | 22 | 18 | 23–66 | 3.8 | 90.8 | **7.2** |
| frame50 | 1272 | 726 | 55.6 | 22 | 18 | 23–66 | 3.8 | 89.0 | 7.3 |
| frame65 | 2670 | 627 | 14.3 | 22 | 16 | 23–66 | 4.0 | 73.8 | 7.6 |
| nomelodia | 762 | 303 | 96.9 | 14 | 11 | 26–63 | 7.4 | 90.7 | 13.7 |
| narrow160 | 878 | 384 | 53.5 | 12 | 10 | 27–50 | 6.6 | 90.8 | 12.4 |
| narrow160nm | 742 | 302 | 35.2 | 9 | 8 | 29–50 | 9.9 | 90.7 | 17.9 |
| narrow250f50 | 636 | 395 | 29.8 | 10 | 6 | 29–58 | 12.8 | 86.4 | 22.3 |
| narrow160f50 | 620 | 383 | 29.8 | 9 | 5 | 29–50 | 14.1 | 86.4 | 24.2 |
| base | 170 | 738 | 8.0 | 4 | 1 | 29–53 | 39.6 | 50.6 | 44.4 |
| strict | 176 | 720 | 8.0 | 4 | 1 | 29–53 | 39.5 | 50.8 | **44.5** |
| narrow160f65 | 175 | 708 | 8.0 | 4 | 1 | 29–48 | 39.8 | 50.8 | 44.6 |
| strict2 | 23 | 1011 | 4.2 | 3 | 0 | 29–53 | 58.3 | 10.6 | 17.9 |

### The same detections, plus merge → deblip → clamp(1 bar) → `force_monophony`

| variant | notes | med_ms | maxpoly | range | prec | rec | **F** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| default-bass | 74 | 819 | 1 | 29–48 | 65.9 | 37.2 | **47.6** |
| frame50 | 150 | 761 | 1 | 24–66 | 30.9 | 32.9 | 31.9 |
| frame65 | 328 | 128 | 1 | 23–66 | 30.6 | 34.9 | 32.6 |
| nomelodia | 95 | 1325 | 1 | 29–61 | 40.1 | 33.4 | 36.5 |
| narrow160 | 109 | 1209 | 1 | 27–50 | 38.9 | 34.6 | 36.6 |
| narrow160nm | 94 | 1186 | 1 | 29–49 | 41.7 | 33.5 | 37.2 |
| narrow250f50 | 115 | 859 | 1 | 29–58 | 41.8 | 38.8 | 40.2 |
| narrow160f50 | 115 | 849 | 1 | 29–50 | 42.6 | 38.9 | 40.7 |
| strict / narrow160f65 | 73 | 824 | 1 | 29–48 | 65.9 | 37.0 | 47.4 |
| strict2 | 19 | 778 | 1 | 29–42 | 72.3 | 10.2 | 17.8 |
| **`bass` (pyin, shipped)** | **162** | **685** | **1** | **28–50** | **75.5** | **82.6** | **78.9** |

Variant settings (frequency bounds in Hz):

| variant | min-note ms | onset | frame | melodia | bounds |
| --- | ---: | ---: | ---: | --- | ---: |
| default-bass | 60 | 0.50 | 0.30 | on | 30–400 |
| frame50 | 60 | 0.50 | 0.50 | on | 30–400 |
| frame65 | 60 | 0.50 | 0.65 | on | 30–400 |
| nomelodia | 60 | 0.50 | 0.30 | off | 30–400 |
| narrow160 | 60 | 0.50 | 0.30 | on | 40–160 |
| narrow160nm | 60 | 0.50 | 0.30 | off | 40–160 |
| narrow160f50 | 100 | 0.50 | 0.50 | off | 40–160 |
| narrow250f50 | 100 | 0.50 | 0.50 | off | 40–250 |
| narrow160f65 | 100 | 0.60 | 0.65 | off | 40–160 |
| base | 125 | 0.60 | 0.65 | off | 35–330 |
| strict | 100 | 0.60 | 0.65 | off | 35–330 |
| strict2 | 125 | 0.70 | 0.70 | off | 35–330 |

Five things to read out of these tables:

1. **Nothing in the parameter space works.** The best Basic Pitch result is
   F 47.6%, against 78.9% for the tracker. And it gets there the wrong way: 65.9%
   precision with only 37% recall — it misses half the part rather than
   transcribing it.
2. **Raising `frame_threshold` alone makes it worse, not better.** `frame65`
   replaced 966 drones with 2670 fragments and peak polyphony *stayed at 22*.
   Lowering it restores the drones. There is no setting at which the model both
   releases notes and holds them for their true duration — the note boundaries
   are wrong, not merely the note count. (Guitar showed the same
   `frame`/`melodia` interaction; here it does not resolve.)
3. **Narrowing the window helps most, and still isn't close.** 30–400 → 40–160 Hz
   is the single most effective lever (F 7.2 → 12.4 raw), because it excludes the
   partials structurally rather than trying to out-threshold them. It plateaus
   around F 40.
4. **`force_monophony` cannot rescue a polyphonic detection.** It resolves
   overlaps by **velocity**, and a bass ghost harmonic is routinely louder than
   its own fundamental, so it drops the correct note more often than the
   incorrect one — raw frame accuracy of 88.4% fell to 30.6% after it ran.
   Lowest-pitch-wins, the obvious alternative, scored 0–20%: the 119.6 s drone at
   MIDI 24 then wins every overlap for the rest of the song.
5. **Ordering inside the cleanup pipeline is load-bearing.** `clamp_sustain` must
   run *before* overlaps are resolved — a multi-second drone wins every overlap
   it spans. Moving it across `force_monophony` swings accuracy ~20 points.

Finding 5 also corrected a stale source comment asserting `force_monophony`'s
position was order-independent. It is not; the comment is fixed where the bass
pipeline is declared.

## The replacement

A bass is a single-line source, so the right tool is a monophonic F0 tracker, not
a polyphonic model with a "keep one note" filter bolted on.

`src/vgt/pyin_notes.py` tracks F0 with pYIN, quantizes each voiced frame to the
nearest semitone, median-filters over 5 frames (~58 ms) to remove pitch jitter
and close one-frame dropouts, then emits each maximal run of one pitch as a note
with velocity from that span's frame RMS.

**Polyphony is 1 by construction, not by enforcement.** Every note boundary is
read from the frame-time grid rather than accumulated as `start + n * hop`, so
consecutive notes share an exact float boundary. (An earlier draft accumulated
them and produced 1-ULP overlaps that reported polyphony 2 — there is a
regression test for this.) That is why `bass` needs no `cap_simultaneous_voices`
or `force_monophony` stage at all.

Shipped settings, in `src/vgt/transcribe.py`:

| Setting | Value | Why |
| --- | --- | --- |
| `BASS_PYIN_FREQUENCY_HZ` | 35–330 Hz | Fundamental search range. 35 Hz sits just below a 5-string's low B (30.9 Hz) while staying off the stem's rumble floor; 330 Hz covers a 24-fret 4-string's top. Both reference estimators put the real line at 29–43 MIDI, well inside. |
| `BASS_PYIN_MINIMUM_NOTE_LENGTH_MS` | 70 ms | Under a 32nd note at 120 BPM, so it discards tracker fragments only. |
| `PYIN_MEDIAN_FILTER_FRAMES` | 5 (~58 ms) | Removes jitter without merging genuine adjacent semitones. |
| `PYIN_HOP_LENGTH` | 256 @ 22050 Hz | ~11.6 ms frames. Onsets are quantized to this. |
| `BASS_SUSTAIN_CLAMP_BARS` | 2.0 | A bass note ringing two bars is a tracker holding through a rest. In bars, not seconds, so slow material isn't clamped tighter. |
| cleanup | merge → deblip → clamp | The ordered subset of the guitar pipeline that still applies; no ghost or voice-cap stage. |

librosa is already a hard vgt dependency, so this adds nothing to install, runs
in-process (no `uvx` subprocess, no model download), and works offline on first
use.

### Result on the same stem

| | notes | maxpoly | max note | prec | rec | **F** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| before (Basic Pitch `bass`) | 966 | 22 | 119.6 s | 3.8 | 90.8 | **7.2** |
| after (pyin `bass`) | 162 | 1 | 4.0 s | 75.5 | 82.6 | **78.9** |

Output range is MIDI 28–50, against the 29–43 core both reference estimators
found.

## Implementation notes worth keeping

- **`PyinSpec` is a separate dataclass, not `BasicPitchSpec` with a different
  `backend` string.** A pyin variant's `settings_hash` must not contain an
  `onset_threshold` or `melodia_trick` that nothing reads — otherwise a project
  profile could "tune" a silent no-op, and `vgt transcription profile show` would
  display fields that do not apply. There is a test asserting those keys are
  absent from the serialized spec.
- **It reuses the existing two-level detection/cleanup cache.** The F0 track is
  by far the expensive part, so `PyinTranscriber` implements `detect_raw` as well
  as `transcribe`; retuning only `cleanup` re-derives without re-tracking. Two
  variants differing only in cleanup share one detection entry, and a pyin
  variant can never be served from a Basic Pitch variant's entry (the identity
  shapes are disjoint).
- **`algorithm_version` stands in for a `package_pin`.** For an in-process
  backend there is no isolated environment to pin. librosa's own version is
  deliberately *not* hashed — a patch-level upgrade must not invalidate every
  user's bass reference — so `PYIN_ALGORITHM_VERSION` must be bumped whenever a
  change here would alter notes from unchanged audio.

## Not changed, and why

- **`vocals` keeps Basic Pitch.** A LALAL vocals stem routinely contains stacked
  backing vocals and harmonies, which are genuinely polyphonic. A monophonic
  tracker would be wrong there for exactly the reason `force_monophony` was never
  applied to it.
- **`guitar` keeps Basic Pitch.** Guitar is legitimately polyphonic, and the
  retune plus five-stage cleanup documented in `guitar-transcription-findings.md`
  did work there.
- **No beat-grid snapping.** Onsets stay at ~11.6 ms frame resolution. `drums`
  authors onto the analyzed beat grid via `vgt.drum_grid`; nothing equivalent is
  applied here.

## Known limitations

- **One stem, one instrument.** Same caveat the guitar findings carry. A synth
  bass, a fretless, a heavily distorted bass, or a drop tuning below 35 Hz are
  all unmeasured, and the frequency bounds plus median-filter width are the
  settings most likely to need revisiting.
- **Chords and double-stops collapse to one note.** A tracker returns one pitch
  per frame by definition. Bleed from another instrument is also resolved to a
  single note rather than ignored.
- **10.9% of graded frames are octave errors** (against 7.3% before). The
  tracker's remaining error mode is picking the wrong partial, not the wrong note
  class — a plausible next improvement, and the reason `oct` is a separate probe
  column rather than folded into `wrong`.
- **Project-local TOML profiles can retune pYIN.** A bass profile may extend
  `bass` or `bass-pyin` to adjust its frequency window, frame settings,
  median filter, note floor, and cleanup recipe. Those output-changing values
  are part of its cache identity, so a changed profile refreshes its matching
  cached detection once.
- **The reference is estimated, not annotated.** Two independent estimators
  agreeing at 85.9% is strong evidence, not ground truth. These figures are a
  reliable *relative* signal for ranking variants and should not be read as
  absolute accuracy.

## Reproducing

`scripts/bass_transcription_probe.py` computes every metric in this document from
a note-events CSV plus the source stem. It runs no backend and writes nothing
into a vgt project:

```sh
uv run python scripts/bass_transcription_probe.py \
  /path/to/project/vgt/<namespace>/transcription/bass/<variant>.csv \
  --stem /path/to/project/vgt/<namespace>/stems/bass.wav \
  --reference cqt --cache /tmp/bass-ref.npz --agreement
```

Pass several CSVs to compare variants side by side; when they share a filename
the containing directory becomes the label, so a sweep laid out as
`sweep/<variant>/bass_basic_pitch.csv` reads directly. `--cache` stores the
computed reference (~40 s for a 3-minute stem) and reuses it across runs.

**Use `--reference cqt` when scoring pyin-backed output** — the default `pyin`
reference would be comparing the tracker to itself. `--agreement` reports how
often the two estimators agree, which is the check that makes either usable.

Column guide: `prec`/`rec`/`f` count every extra simultaneous pitch as a false
positive; `hit`/`oct`/`wrong`/`miss` are shares of graded frames (stem RMS above
the 25th percentile, so inter-phrase silence is excluded). `oct` is split out from
`wrong` because an octave error is a different fix than a mistaken pitch.
**Always read `f`, never `rec`** — see "The complaint, quantified" for why.

To regenerate the Basic Pitch sweep, install the backend once and drive it
through the same command line `build_basic_pitch_argv` produces:

```sh
uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"
basic-pitch sweep/strict /path/to/stems/bass.wav \
  --model-serialization onnx --save-note-events --midi-tempo 120.004 \
  --minimum-frequency 35 --maximum-frequency 330 \
  --minimum-note-length 100 --onset-threshold 0.6 --frame-threshold 0.65 --no-melodia
```

The pyin row needs no sweep harness — it is what `vgt analyze` produces for
`bass`, and `PyinTranscriber().transcribe(...)` can be called directly against a
stem for a one-off.
