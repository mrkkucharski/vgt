# Guitar transcription findings

Status: **implemented 2026-07-22** for `guitar_type: acoustic`.
`src/vgt/transcribe.py` now applies the acoustic overrides and the
sustain-clamp/harmonic-ghost/voice-cap cleanup pass described below whenever
`guitar_type == "acoustic"`; `electric` and unset are deliberately left at the
original shared defaults, since only the acoustic case was measured (see "The
frequency bounds ignore `guitar_type`" and "Proposed changes" below, now
applied rather than proposed). This changes `guitar`'s `settings_hash` for
acoustic-declared projects, so existing acoustic transcriptions correctly
invalidate and re-transcribe on the next run — no migration needed. It does
**not** change any other target's hash or output.

Investigated 2026-07-22 after a user report that a real project's guitar MIDI
was unusable.

## The complaint, quantified

The subject is `7Rivers`, a 178 s track at 120 BPM, transcribed with the
shipping defaults (`onset 0.5`, `frame 0.3`, `min-note 60 ms`, melodia on,
70–1400 Hz) from the LALAL `guitar` stem.  Its recorded `note_count` is 2907.

The instrument is a **steel-string acoustic**, strummed more or less
continuously, and the project correctly declares `guitar_type: acoustic`, so
LALAL's acoustic model produced the stem.  This matters for reading everything
below: the failure is not a distorted-guitar edge case.  Basic Pitch's training
corpus includes GuitarSet, which is acoustic guitar, so this is close to
material the model should handle well — which makes the result below worse than
it first appears, not better.

| Symptom | Measurement |
| --- | --- |
| Sustain runaway | 163 notes longer than 5 s; the longest, a C#2, runs **126 s** |
| Impossible polyphony | **22 simultaneous voices** for 65 % of the song, peaking at 26 |
| Harmonic ghosts | **65 %** of sounding notes sit an octave/12th/double-octave above another note sounding at the same instant |
| Wrong notes | Only **52.5 %** of note-time lands on a tone of the chord vgt itself detected underneath |

The last row is the one that matters.  The output is not merely verbose — at
52.5 % chord agreement it is close to uninformative, so no amount of muting or
filtering in REAPER recovers a usable practice reference from it.

## Root cause

Two shipping defaults interact badly with a continuously strummed acoustic:

- **`DEFAULT_FRAME_THRESHOLD = 0.3`** — a strummed steel-string rings out for
  seconds, and successive chords overlap that ring-out, so the frame
  activations for a pitch rarely fall below 0.3 even after the player has moved
  on.  Notes are therefore never released.  This is what produces the drones
  and, with them, the 22-voice floor.
- **`DEFAULT_MELODIA_TRICK = True`** — melodia then bridges the surviving gaps,
  gluing separate detections into single multi-minute notes.

`DEFAULT_MINIMUM_NOTE_LENGTH_MS = 60` is a secondary contributor: it admits 373
sub-100 ms detections that sit underneath the drones.

The harmonic ghosts have a matching acoustic explanation: a steel-string's
octave and twelfth partials are strong enough, especially in a six-string
strum, to be detected as notes in their own right.  They survive at 30 % even
in the best retuned variant, which is why the cleanup stage below is not
optional.

The frequency bounds are **barely** implicated — see the next section.  The
ghosts are partials of notes *inside* the band, not content above it.

Two further hypotheses were tested and ruled out:

- **Stem quality.** The `guitar` stem is above −40 dBFS-peak for 99 % of its
  length, and only 1.1 % of baseline note-time falls over the near-silent
  remainder.  A silence gate would gain essentially nothing here, and the mess
  is not LALAL bleed.
- **Post-processing alone.** Applying the cleanup described below to the
  *baseline* output still leaves 2077 notes at a 255 ms median.  Polyphony
  becomes legal but the notes remain wrong, because the underlying detections
  are noise.  The settings must be fixed first; cleanup cannot substitute.

## The frequency bounds ignore `guitar_type` (small, but wrong)

`_TARGET_FREQUENCY_HZ["guitar"] = (70.0, 1400.0)` is keyed by target only, and
its comment justifies the floor as *"below drop/Eb-tuned E2"* — reasoning about
an electric that does not apply to a standard-tuned acoustic, whose lowest note
is E2 at 82.4 Hz.  Meanwhile `vgt` already **requires** the caller to declare
`--guitar electric|acoustic`, stores it as `analysis.stems.guitar_type`, and
uses it to pick the LALAL model — so the information needed to narrow the band
correctly is on hand and simply unused.

The baseline output contains 31 notes below E2, worth 2.5 % of note-time,
including part of the 126 s C#2 drone.  These are provably not guitar notes.

But narrowing the band is worth very little on its own:

| variant | bounds | %chordtone |
| --- | --- | ---: |
| baseline | 70–1400 | 52.5 |
| ac_baseline | 80–1200 | 54.1 |
| f65_o60 | 70–1400 | 67.4 |
| ac_f65_o60 | 80–1400 | 67.6 |
| ac_bounds | 80–1200 | 68.0 |

**+1.6 points at baseline settings, +0.6 on top of a retune.**  Worth doing as
a correctness cleanup — a transcription should not contain notes the declared
instrument cannot play — but it is not a fix and should not be sold as one.
Tightening the ceiling to 1000 Hz likewise changed nothing measurable
(`tight` vs `tight_lowmax` below).

## Parameter sweep

Run against the same stem, `basic-pitch[onnx]==0.4.0`, ONNX serialization,
`--midi-tempo 120.004`, 70–1400 Hz unless noted.  `%ghost` is the harmonic-ghost
share, `%>6vc` the share of sounding time above six voices.

| variant | notes | med_ms | max_s | >5s | maxpoly | medpoly | %>6vc | %ghost | %chordtone |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline (shipping) | 2907 | 383 | 126.3 | 163 | 26 | 22 | 100 | 65.4 | 52.5 |
| ac_baseline | 2898 | 372 | 101.6 | 145 | 23 | 20 | 100 | 64.0 | 54.1 |
| nomelodia | 2697 | 337 | 101.6 | 135 | 21 | 19 | 100 | 62.5 | 57.4 |
| frame60 | 3942 | 384 | 21.6 | 100 | 25 | 21 | 99 | 60.3 | 37.5 |
| frame60_nomel | 2596 | 267 | 14.0 | 21 | 20 | 8 | 71 | 43.0 | 54.3 |
| tight | 1113 | 534 | 21.1 | 23 | 17 | 5 | 34 | 36.3 | 63.0 |
| tight_lowmax | 1125 | 534 | 10.7 | 21 | 16 | 5 | 34 | 36.5 | 62.6 |
| **f65_o60** | 1060 | 489 | 6.7 | 6 | 14 | 4 | 15 | 30.2 | **67.4** |
| ac_f65_o60 | 1058 | 489 | 6.7 | 6 | 14 | 4 | 14 | 30.2 | 67.6 |
| **ac_bounds** | 1060 | 489 | 6.7 | 6 | 14 | 4 | 14 | 30.2 | **68.0** |
| **f70_o65** | 627 | 499 | 4.5 | 0 | 14 | 2 | 2 | 22.9 | **75.6** |
| tighter | 433 | 557 | 4.5 | 0 | 12 | 1 | 1 | 17.7 | 78.1 |

Variant settings (all else at the shipping defaults):

| variant | min-note ms | onset | frame | melodia | max Hz |
| --- | ---: | ---: | ---: | --- | ---: |
| baseline | 60 | 0.50 | 0.30 | on | 1400 |
| ac_baseline | 60 | 0.50 | 0.30 | on | 1200 (min 80) |
| nomelodia | 60 | 0.50 | 0.30 | off | 1400 |
| frame60 | 60 | 0.50 | 0.60 | on | 1400 |
| frame60_nomel | 60 | 0.50 | 0.60 | off | 1400 |
| tight | 125 | 0.60 | 0.60 | off | 1400 |
| tight_lowmax | 125 | 0.60 | 0.60 | off | 1000 |
| f65_o60 | 100 | 0.60 | 0.65 | off | 1400 |
| ac_f65_o60 | 100 | 0.60 | 0.65 | off | 1400 (min 80) |
| ac_bounds | 100 | 0.60 | 0.65 | off | 1200 (min 80) |
| f70_o65 | 125 | 0.65 | 0.70 | off | 1400 |
| tighter | 125 | 0.70 | 0.70 | off | 1400 |

Three things to read out of this table:

1. **Chord agreement rises monotonically with tightening**, from 52.5 % to
   78.1 %.  Retuning is not just deleting notes, it is deleting the *wrong*
   notes — otherwise agreement would stay flat as the count fell.
2. **`frame60` is worse than baseline** (37.5 %).  Raising the frame threshold
   while leaving melodia on is actively harmful: releases now happen, and
   melodia reconnects them into more, shorter, wronger notes.  The two settings
   must move together, which is why `nomelodia` alone is also insufficient.
3. **Even the best variants exceed six voices** (`f65_o60` peaks at 14).  No
   Basic Pitch setting fixes this; it needs the cleanup stage below.

`tighter` scores highest on chord agreement but at 433 notes over 178 s is
likely dropping real passing notes, and its score is partly an artifact of
keeping only the most confident (therefore most chord-tone-ish) detections.
`f65_o60` and `f70_o65` are the honest candidates.

## Changes applied

All four proposals below are now implemented in `src/vgt/transcribe.py`,
gated on `guitar_type == "acoustic"` throughout:

1. **Retuned guitar defaults, acoustic only.** `GUITAR_ACOUSTIC_ONSET_THRESHOLD
   = 0.6`, `GUITAR_ACOUSTIC_FRAME_THRESHOLD = 0.65`,
   `GUITAR_ACOUSTIC_MINIMUM_NOTE_LENGTH_MS = 100.0`,
   `GUITAR_ACOUSTIC_MELODIA_TRICK = False`. These are separate constants from
   the shared `DEFAULT_*` ones, applied only inside `default_spec_for_target`
   when `target == "guitar" and guitar_type == "acoustic"` — `electric` and
   unset fall through to the original shared defaults untouched, and every
   other target's `settings_hash` is unaffected (verified by
   `test_default_spec_leaves_electric_and_unset_guitar_at_the_generic_defaults`
   and `test_default_spec_acoustic_override_is_guitar_only`).

   This is still measured on one acoustic track only. Whether a distorted
   electric wants the same numbers, or different ones, remains unmeasured —
   `guitar_type: electric` intentionally does not get these thresholds.

2. **`guitar_type` (and `time_signature`) now reach the spec.**
   `default_spec_for_target` takes both; `TargetTranscriberRouter.spec_for_target`
   and the `TranscriberRouter` protocol thread them through;
   `analysis._refresh_target` reads `analysis.stems.guitar_type` and
   `analysis.tempo.value.time_signature` and passes them down. This is what
   lets 80–1200 Hz apply only to a declared acoustic guitar, and lets the
   sustain clamp below convert bars to seconds at the song's actual tempo and
   signature.

3. **Post-transcription cleanup pass**, run inside
   `BasicPitchTranscriber.transcribe` whenever the spec requests it:
   `_clamp_sustain` → `_drop_harmonic_ghosts` → `_cap_simultaneous_voices`
   (that order — the clamp must run first or a still-runaway drone would
   dominate which voices the cap retires; the ghost drop runs on
   already-clamped durations). The voice cap truncates the quietest
   **already-sounding** voice at a new note's onset rather than deleting
   it outright, so the onset still lands on the reference track — it does not
   reject a new note for being the quieter arrival, since by the time a note
   is a candidate for the cut it has already survived the ghost drop.

   The clamp went from a fixed 4 s to `GUITAR_SUSTAIN_CLAMP_BARS = 2.0` bars,
   resolved to seconds via `_bar_duration_seconds(bpm, time_signature)` at
   spec-construction time (defaulting to 4/4 when the signature is missing,
   same fallback `tempo.py` already uses) — the generalisation flagged below
   the original measurement, so slower material isn't clamped tighter than
   this track was.

4. **Six-voice limit enforced unconditionally** whenever cleanup runs, via
   `GUITAR_MAX_SIMULTANEOUS_VOICES = 6` and `_cap_simultaneous_voices` — not a
   configurable knob, so nothing downstream can emit a >6-voice guitar chord
   once acoustic cleanup is active.

Both the retuned thresholds and the cleanup fields live on `BasicPitchSpec`
itself (three new optional fields, `None`/`False` for every non-guitar
target), so a future change to any of them naturally invalidates
`settings_hash` and forces re-transcription — no separate cache-key needed.
Adding the fields did, once, bump every existing basic-pitch target's stored
hash (the dataclass gained columns), which is an expected one-time,
zero-cost re-transcription, not a correctness concern.

Test coverage: `tests/test_transcribe.py` covers the acoustic-vs-electric spec
split, the bar-to-seconds conversion (including the no-tempo-yet case), each
cleanup function in isolation (including the "new note quieter than what it
displaces" edge case the eviction rule does *not* handle, and the "already
truncated voice must stay truncated for later overlap checks" case), the
full pipeline ordering, and one end-to-end `BasicPitchTranscriber.transcribe`
run against a fake subprocess producing a runaway note, verifying both the
rewritten CSV and MIDI reflect the cleaned result.

## Real-world verification: `7Rivers` re-transcribed

After implementing, `7Rivers`'s guitar target was reset
(`vgt analyze --forget-transcription guitar`, then `--transcribe guitar`) and
re-transcribed against the real acoustic stem through the normal CLI path —
not the sweep script. Before/after, measured by
`scripts/guitar_transcription_probe.py` against the actual sidecar output:

| | notes | med_ms | max_s | >5s | maxpoly | %ghost | %chordtone |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| before (this doc's baseline) | 2907 | 383 | 126.3 | 163 | 26 | 65.4 | 52.5 |
| after (production output) | 897 | 488 | **4.0** | **0** | **6** | 19.6 | **70.5** |

Every invariant holds exactly: no note exceeds the 4 s (2-bar, 120 BPM) clamp,
and polyphony never exceeds 6. Chord agreement (70.5 %) came out slightly
*above* the sweep's own `ac_bounds` measurement (69.9 %, prototype cleanup on
the `f65_o60` sweep variant) — consistent with, not merely close to, the
sweep's prediction. `settings_hash` changed as expected
(`b679a47b5e...` → `dd64f73e47...`), so no manual sidecar edit was needed:
`--forget-transcription` deleted the stale `guitar.mid`/`.csv` and the
persisted entry, and the ordinary transcription stage detected the missing
target and re-ran it against the new spec. Only `guitar`'s entry changed;
`drums` reported `unchanged, using cached result` in both runs.

## Reproducing

`scripts/guitar_transcription_probe.py` computes every metric in this document
from a Basic Pitch note-events CSV.  It runs no model and writes nothing into a
vgt project:

```sh
uv run python scripts/guitar_transcription_probe.py \
  /path/to/project/vgt/<namespace>/transcription/guitar.csv \
  --chords /path/to/project/vgt/<namespace>/chords.txt
```

Passing several CSVs compares parameter variants side by side; when they share
a filename the containing directory is used as the label, so a sweep laid out
as `sweep/<variant>/guitar_basic_pitch.csv` reads directly.

To regenerate the sweep, install the backend once and drive it through the same
command line `build_basic_pitch_argv` produces:

```sh
uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"
basic-pitch sweep/f65_o60 /path/to/stems/guitar.wav \
  --model-serialization onnx --save-note-events --midi-tempo 120.004 \
  --minimum-frequency 70 --maximum-frequency 1400 \
  --minimum-note-length 100 --onset-threshold 0.6 --frame-threshold 0.65 --no-melodia
```

Setting `VGT_BASIC_PITCH_CMD=basic-pitch` afterwards points vgt at that
installed binary and skips the ~35 s cold `uvx` build per run — the escape
hatch `BASIC_PITCH_CMD_ENV` already documents.

The chord-agreement metric depends on vgt's own `chords.txt`, which is itself
estimated and collapsed to `maj_min`.  It is a useful *relative* signal for
ranking variants and should not be read as an absolute accuracy figure.
