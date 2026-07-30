# Bass transcription findings

Why `bass` uses a monophonic pitch tracker (pYIN) instead of Basic Pitch, and
what was measured to establish that. Companion to
`docs/guitar-transcription-findings.md`, which does the same for the acoustic
guitar profile. The code this justifies lives in `src/vgt/pyin_notes.py`.

Everything below was measured on the 7Rivers bass stem
(`vgt/<ns>/stems/bass.wav`, 178.6 s, LALAL-separated from the original mix),
at the project's detected 120.004 BPM 4/4.

## The failure

The original `bass` profile was Basic Pitch with a 30–400 Hz window and **no
cleanup pipeline at all**. Its output:

| Metric | Value |
| --- | --- |
| Notes | 966 |
| Peak simultaneous voices | **22** |
| Share of the song with ≥17 voices sounding | **98%** |
| Longest note | **119.6 s** |
| Notes longer than 4 s | 161 (73 longer than 10 s) |
| Share of all note-seconds from notes >4 s | 83% |
| Pitch range | MIDI 23–66 (B0–F♯4) |

Basic Pitch latched onto the stem's sustained low-frequency energy and emitted a
permanent chord under the whole track — pitches 24, 26, 54, 55, 56, 58, 61, 63
and 66 all held for 45–120 s, each at velocity 78–95. This is the same drone
mechanism documented for acoustic guitar (a low `frame_threshold` never sees an
activation drop, and melodia bridges the surviving gaps), but far worse, because
a bass stem is almost entirely sustained low-frequency energy.

## What the stem actually plays

Two estimators from different algorithm families, so neither's failure modes
explain the other:

- **pYIN** — time-domain autocorrelation with a probabilistic voicing model
  (`librosa.pyin`, 35–400 Hz).
- **CQT harmonic sum** — frequency-domain: constant-Q magnitude spectrogram,
  scored per semitone by a weighted sum of energy at the fundamental, octave,
  12th and double octave.

| Method | 1st pct | median | 99th pct |
| --- | --- | --- | --- |
| pYIN | 28.9 | **34.0** | 42.2 |
| CQT harmonic sum | 29 | **34** | 44 |

They agree on **85.6%** of loud frames (energy above the 25th percentile). pYIN
finds the stem voiced 87.6% of the time. The real line is MIDI ~29–43, median 34
(B♭1) — so 524 of Basic Pitch's 966 notes sat above that range, and 140 were
above MIDI 48 entirely.

## Tuning could not fix it

Eleven Basic Pitch inferences were run over the same stem, sweeping
`onset_threshold` (0.5–0.7), `frame_threshold` (0.30–0.70),
`minimum_note_length` (60–125 ms), `--no-melodia`, and four frequency ceilings
(160/250/330/400 Hz). Each was then put through every ordering of the existing
cleanup stages, including `force_monophony`, and scored against the independent
CQT reference. Best single-line results:

| Detection | notes | hit | octave err | missed |
| --- | --- | --- | --- | --- |
| default `bass` + cleanup + monophony | 120 | 34.3% | 7.8% | 32.2% |
| 40–160 Hz, no melodia, + cleanup | 103 | 33.6% | 12.9% | 32.7% |
| onset 0.6 / frame 0.65, + cleanup | 76 | 37.2% | 7.8% | 49.8% |
| **pYIN + segmentation** | 205 | **83.8%** | 2.0% | **2.7%** |

Nothing in the parameter space cleared ~37%. Three reasons:

1. **The note boundaries are wrong, not just the note set.** Raising
   `frame_threshold` to 0.65 replaced 966 drones with 2670 fragments (peak
   polyphony still 22). Lowering it restored the drones. There is no setting
   where the model both releases notes and holds them for their real duration.
2. **`force_monophony` picks the loudest note, and on bass that is usually the
   wrong one.** A ghost harmonic is routinely louder than its own fundamental,
   so the stage dropped the correct note more often than the incorrect one:
   88.4% raw frame accuracy fell to 30.6% after it ran.
3. **Lowest-pitch-wins is worse.** The obvious fix — a bass ghost is above its
   fundamental, so keep the lowest — scored 0–20%, because the 119 s drone at
   MIDI 24 then wins every overlap for the rest of the song. Clamping sustain
   *before* resolving overlaps helps, but only back to ~34%.

Finding 3 also corrects a stale claim in the source: a comment asserted
`force_monophony`'s pipeline position was order-independent. It is not — moving
`clamp_sustain` across it swings accuracy by ~20 points. The ordering is now
documented where the bass pipeline is declared.

## The replacement

A bass is a single-line source, so the right tool is a monophonic F0 tracker,
not a polyphonic model with a "keep one note" filter bolted on. `pyin_notes.py`
tracks F0 with pYIN, quantizes to semitones, median-filters (5 frames ≈ 58 ms)
to remove jitter and close one-frame dropouts, and emits each maximal run as a
note with velocity from frame RMS.

Notes are **non-overlapping by construction** — every boundary is read from the
frame-time grid rather than accumulated — so `bass` needs no
`cap_simultaneous_voices` or `force_monophony` stage. Its cleanup is only the
ordered subset that still applies: `merge_fragments`, `drop_isolated_notes`,
`clamp_sustain`.

librosa is already a hard vgt dependency, so this adds nothing to install and
runs in-process, offline, with no `uvx` subprocess and no model download.

### Result on the same stem

Frame-level precision/recall against the independent CQT reference, counting
every extra simultaneous pitch as a false positive (which is what the "hit"
column above deliberately does *not* do — with 22 voices sounding, something
almost always matches, which is why raw Basic Pitch scores a meaningless 90.8%
recall):

| | notes | polyphony | max note | precision | recall | F |
| --- | --- | --- | --- | --- | --- | --- |
| Old `bass` (Basic Pitch) | 966 | 22 | 119.6 s | 3.8% | 90.8% | **7.2%** |
| New `bass` (pYIN) | 162 | 1 | 4.0 s | 75.5% | 82.7% | **78.9%** |

Resulting pitch range is MIDI 28–50, against the 29–43 core both reference
estimators found.

## Profiles

`bass` now resolves to the tracker. The Basic Pitch profiles are retained under
explicit names so the old behaviour stays reachable for comparison, and so an
existing sidecar naming one still resolves:

| Profile | Backend | Notes |
| --- | --- | --- |
| `bass` | pyin | The default |
| `bass-pyin` | pyin | Same settings, explicit name |
| `bass-basic-pitch` | basic-pitch | The retired default, unchanged (30–400 Hz, no cleanup) |
| `bass-monophonic` | basic-pitch | `bass-basic-pitch` plus `force_monophony` |

Moving the default changes `bass`'s `settings_hash`, so the next analysis
re-transcribes bass once. `bass-basic-pitch`'s hash is deliberately identical to
the old `bass` hash, which `tests/test_transcribe.py` pins.

## Open questions

- **Only one stem was measured.** The guitar findings carry the same caveat.
  Both frequency bounds and the median-filter width are the settings most likely
  to need revisiting on a different bass sound (synth bass, fretless, heavy
  distortion, drop tuning below 35 Hz).
- **`vocals` was not changed.** A LALAL vocals stem routinely contains stacked
  backing vocals and harmonies, which are genuinely polyphonic; a monophonic
  tracker would be wrong there for the same reason `force_monophony` was never
  applied to it.
- **Onset timing is frame-quantized** to ~11.6 ms. That is inside reading
  tolerance for a reference track but coarser than an onset detector would give,
  and no attempt is made to snap notes to the analyzed beat grid the way
  `vgt.drum_grid` does for drums.
- **Project-local TOML profiles cannot extend a pyin profile.** `extends` still
  requires a Basic Pitch base, so the tracker's settings are builtin-only.
