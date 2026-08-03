# Bass transcription findings

> Part of the per-instrument transcription evidence indexed in
> [instrument-transcription-findings.md](instrument-transcription-findings.md),
> which also carries the shared measurement method and the lessons that
> generalize across instruments.

Status: **implemented 2026-07-29/30**, extended 2026-07-30 with re-articulation
splitting (see that section — it is the one failure the F-measure below cannot
see). `bass` no longer uses Basic Pitch at all.
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

Eleven **Basic Pitch experiments** over the same stem, `basic-pitch[onnx]==0.4.0`,
ONNX serialization, `--midi-tempo 120.004`. These historical rows explain why
the old backend was retired; they are not production settings. Scored against
the **CQT** reference (scoring against pYIN would be circular for the final
shipped pYIN row).

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
| `BASS_PYIN_FREQUENCY_HZ` | 35–330 Hz | Fundamental search range. 35 Hz is **higher** than a five-string low B (30.9 Hz), so that fundamental and lower/drop-tuned material are unsupported by this production profile. 330 Hz covers a 24-fret 4-string's top. Both reference estimators put the measured line at 29–43 MIDI, well inside. |
| `BASS_PYIN_MINIMUM_NOTE_LENGTH_MS` | 70 ms | Under a 32nd note at 120 BPM, so it discards tracker fragments only. |
| `PYIN_MEDIAN_FILTER_FRAMES` | 5 (~58 ms) | Removes jitter without merging genuine adjacent semitones. |
| `PYIN_HOP_LENGTH` | 256 @ 22050 Hz | ~11.6 ms frames. Onsets are quantized to this. |
| `PYIN_REARTICULATION_SPAN_FRAMES` | 2 (~23 ms) | Window the envelope rise is measured over. See "Re-articulation" below for the sweep. |
| `PYIN_REARTICULATION_RISE_DB` | 0.8 dB | Energy rise that marks the string being plucked again at an unchanged pitch. Deliberately low; the spacing rule supplies the precision. |
| `PYIN_REARTICULATION_MINIMUM_SPACING_BEATS` | 0.375 | Closest two cuts inside one pitch run may fall. In beats, so it scales with tempo. |
| `BASS_SUSTAIN_CLAMP_BARS` | 2.0 | A bass note ringing two bars is a tracker holding through a rest. In bars, not seconds, so slow material isn't clamped tighter. |
| cleanup | merge → deblip → clamp | The ordered subset of the guitar pipeline that still applies; no ghost or voice-cap stage. |

librosa is already a hard vgt dependency, so this adds nothing to install, runs
in-process (no `uvx` subprocess, no model download), and works offline on first
use.

### Result on the same stem

This is the final production result, not one of the preceding Basic Pitch
experiments. It is measured on one 7Rivers separated stem against the estimated
CQT reference, so it is a relative comparison rather than a general accuracy
guarantee.

| | notes | maxpoly | max note | prec | rec | **F** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| before (Basic Pitch `bass`) | 966 | 22 | 119.6 s | 3.8 | 90.8 | **7.2** |
| after (pyin `bass`) | 162 | 1 | 4.0 s | 75.5 | 82.6 | **78.9** |

Output range is MIDI 28–50, against the 29–43 core both reference estimators
found.

## Re-articulation: the half of the part the tracker could not see

Status: **implemented 2026-07-30**, `PYIN_ALGORITHM_VERSION` 1 → 2. Bumps
`bass`'s `settings_hash`, so an existing bass transcription invalidates and
re-transcribes once. No other target is touched.

Reported by the user after hand-correcting the reference against the audio in
REAPER: the transcript held one long note where the recording plays the same
note several times. "Individual notes played on the same string" is exactly the
failure — and the section above, with its 78.9% F-measure, cannot see it at all.

### Why every metric above is blind to it

An F0 tracker reports pitch. Playing one fret four times running does not change
pitch, so it is one continuous F0 run, and `segment_notes`' "each maximal run of
one pitch becomes a note" rule — the same rule that makes polyphony 1 by
construction — glues those four notes into one. Every metric in this document is
frame-level, asking *which pitch is sounding now*, and the right pitch is
sounding the whole time. The before/after table below shows this directly: `hit`,
`oct`, `wrong`, `miss`, `prec`, `rec` and `f` are unchanged **to the decimal**
across a change that recovers a third of the notes in the part.

Nor could the estimated reference have caught it. Both reference estimators
(pYIN and CQT harmonic sum) are frame-level and share the blind spot precisely.
This is the one thing in this document that an estimated reference could not
have established, and it took a human annotation to see.

### The reference

The maintainer's hand-corrected `[clean] Bass Ref — default (MIDI)` track,
parsed out of the RPP and committed as `tests/fixtures/bass_7rivers/` (numbers
only, no audio — see that directory's README). **272 notes over the full
178.6 s**, reviewed against the audio in REAPER.

This is the project's first full-length hand annotation for any instrument, and
it is what makes the rest of this section trustworthy rather than suggestive.
An earlier prefix-only version of it (117 notes, 64.7 s) is what the first
tuning was fitted to.

**83% of the reference (225 of its 272 notes) sits inside a run of repeated
notes on one pitch** — runs of 3 are the most common, and the longest is 9.
This part is overwhelmingly rhythmic same-note playing, which is why
re-articulation dominates its score and why the frame-level metrics say nothing
useful about it.

### Root cause and fix

`pyin_notes._rearticulation_frames` recovers the missing onsets from the
envelope rather than the pitch: each pluck restarts the decay, so log frame
energy jumps. It reuses the per-frame RMS `track_f0` already returns for
velocity, so it costs no second pass over the audio and no new dependency. A
frame qualifies when energy has risen by `rise_db` over the previous
`span_frames` frames *and* that rise is a local maximum.

**Two thresholds, doing different jobs.** The rise threshold alone cannot work:
a re-attack the maintainer marks is typically *faint* — median rise 0.66 dB,
against 2.16 dB for an onset the tracker already found — so a threshold high
enough to be self-sufficient misses two thirds of them, and one low enough to
catch them fires everywhere. So `segment_notes` also enforces a **minimum
spacing between cuts** inside one run, which is where the precision comes from:
below a dotted sixteenth, two detections are far more often one attack found
twice than two notes played. Dropping the threshold alone (1.0 → 0.8 dB) gains
0.3 F; adding the spacing rule takes it to +2.4, improving both folds.

The spacing is expressed in **beats**, not milliseconds, for the same reason
`BASS_SUSTAIN_CLAMP_BARS` is: a fixed millisecond value would block genuine
repeated notes at a fast tempo and permit double-triggers at a slow one.
`PyinSpec` already carries `midi_tempo`, so this adds no new dependency.

### Five detectors that did not work

All measured against the full 272-note reference, scored through the real
pipeline. This section exists so nobody re-runs them.

| Approach | Best F | Why it failed |
| --- | ---: | --- |
| **Shipped** (envelope rise + spacing) | **75.6** | — |
| Beat-grid-guided candidates | 74.2 | Real but small gain, and it would make the analyzed beat grid part of bass's cache identity — a tempo re-analysis would then invalidate every bass transcription. Not worth +1.0. |
| Globally adaptive threshold | 72.0 | The outro's attacks are genuinely softer (median rise 0.95 dB vs 1.6–1.7 dB earlier), so local normalization looked obvious. It underperforms a constant threshold at every window/delta tried. |
| Shorter / band-limited RMS envelope | 73.0 | pYIN's own 93 ms RMS window smears a 20 ms pluck across eight hops, so a sharper envelope looked obviously better. It is not: the smearing is useful smoothing, and every shorter window (256/512/1024) and band (80–800, 150–1500, 200–2500, 300–4000 Hz) scored lower. |
| Decaying peak follower | 70.2 | The physically-motivated model — a plucked note decays, so a re-attack is energy departing upward from the decay. Worse at every release rate from 3 to 45 dB/s. A separated stem's decay is not clean enough. |
| Mel-band spectral flux | 27 | A bass stem has almost no high-frequency content for flux to key on. Fires 237 times in 15 s, or 3. |

The lesson is that the crude measure won. Four of the five alternatives were
better-motivated *models* of the physics, and all four lost to differencing a
smoothed energy envelope and then constraining where the results may land.

### Sweep

Scored through the real backend plus the shipped cleanup, onset F at a 50 ms
tolerance, one-to-one nearest matching. Folds A/B are alternating 15 s blocks,
so a row that wins only by fitting one passage shows as a gap between them.

| span | rise dB | spacing (beats) | notes | P | R | **F** | foldA | foldB |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| — | off | — | 162 | 76.5 | 45.6 | **57.1** | 61.3 | 52.6 |
| 2 | 1.0 | 0 (previous ship) | 255 | 75.7 | 71.0 | **73.2** | 76.9 | 69.5 |
| 2 | 0.8 | 0 | 286 | 71.7 | 75.4 | 73.5 | 76.3 | 70.5 |
| 2 | 0.7 | 0.3125 | 290 | 72.1 | 76.8 | 74.4 | 76.8 | 71.8 |
| 2 | 0.7 | 0.5 | 266 | 76.3 | 74.6 | 75.5 | 77.5 | 73.3 |
| 2 | 0.75 | 0.375 | 275 | 74.9 | 75.7 | 75.3 | 77.3 | 73.2 |
| **2** | **0.8** | **0.375** | **268** | **76.1** | **75.0** | **75.6** | **77.4** | **73.6** |
| 2 | 0.8 | 0.5 | 252 | 78.6 | 72.8 | 75.6 | 77.9 | 73.2 |
| 2 | 0.9 | 0.375 | 249 | 78.7 | 72.1 | 75.2 | 78.1 | 72.2 |
| 3 | 0.9 | 0.375 | 275 | 74.2 | 75.0 | 74.6 | 77.9 | 71.0 |

Shipped: **span 2 (~23 ms), 0.8 dB, 0.375 beats.** The plateau runs 0.75–0.8 dB
× 0.3125–0.5 beats at F 75.0–75.6; 0.375 beats is chosen over 0.5 for its higher
recall at the same F, since recovering the played notes is the point. Both folds
improve over both the no-split and the previous shipped row, so the gain is not
one passage.

### Result

Through the real CLI (`vgt analyze --transcribe-only bass`) on the same stem:

| | notes | matched | onset P | onset R | **onset F** |
| --- | ---: | ---: | ---: | ---: | ---: |
| no splitting (`PYIN_ALGORITHM_VERSION` 1) | 162 | 124/272 | 76.5 | 45.6 | **57.1** |
| first splitting (1.0 dB, no spacing) | 255 | 193/272 | 75.7 | 71.0 | **73.2** |
| **shipped** (0.8 dB + 0.375-beat spacing) | **268** | **204/272** | **76.1** | **75.0** | **75.6** |

Recall is the headline: the tracker found **46%** of the notes played and now
finds 75%. Requiring the right pitch as well as the right time costs 0.8 points
(F 74.8), so timing, not pitch, is what remains wrong.

The output's note count now matches the reference closely (268 against 272), as
does its duration profile — the six longest notes are 4.00/3.11/3.02/1.97/1.88/
1.87 s against the reference's 4.00/3.72/3.04/1.97/1.88/1.88 s.

Every frame-level figure is **unchanged** — same 78.9% F, 10.9% octave errors,
28–50 range, polyphony 1 — as the argument above predicts.

### Where it is still weakest

Accuracy falls steadily through the song:

| span | reference notes | detected | P | R | F |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0–60 s | 114 | 107 | 87.9 | 82.5 | **85.1** |
| 60–120 s | 104 | 105 | 71.4 | 72.1 | **71.8** |
| 120–180 s | 54 | 56 | 62.5 | 64.8 | **63.6** |

Note *counts* track the reference closely in all three, so this is a timing and
placement problem, not a count problem — the later sections play softer, more
legato re-attacks, and the detector puts about the right number of notes in
approximately the wrong places.

### A cache bug this work exposed

The first implementation added `rearticulation_*` to `PyinSpec` and therefore to
`settings_hash`, but **not** to `transcription_variants.detection_identity`.
Re-articulation splitting happens inside `segment_notes`, so it shapes the raw
note list — a changed setting must invalidate the *detection* cache, not only
the derived variant. It did not, so retuning the splitter and re-running printed
`transcription — bass/default: unchanged, using cached result` and kept the old
notes.

It went unnoticed on the first change only because `PYIN_ALGORITHM_VERSION` went
1 → 2 in the same commit, and that *is* in the detection identity. A project
profile retuning `rearticulation_rise_db` would have silently done nothing —
exactly the "tuning a silent no-op" failure `PyinSpec`'s docstring warns about.
Fixed, with a test asserting each of the three settings moves `detection_hash`.

### Known limitations of the split

- **The spacing rule blocks fast repeated notes.** A dotted sixteenth is 188 ms
  at 120 BPM, so genuine sixteenth-note repeats on one pitch cannot be split.
  On this track that costs little — only 17 of 271 reference inter-onset gaps
  are under 150 ms — but a busier bass part would need it lowered.
- **A re-attack quieter than 0.8 dB is still missed**, and a swell inside one
  held note louder than that is a spurious split. There is no articulation
  model here, only an envelope threshold plus a spacing constraint.
- **One track, one bassist.** The reference is 272 notes of one part in one
  style. Nothing here is validated against a different player, tuning, or genre.

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
  bass, a fretless, or a heavily distorted bass is unmeasured. Material below
  35 Hz — including a five-string low B fundamental at 30.9 Hz and lower drop
  tunings — is outside the configured production range, not merely unmeasured.
  New reference measurements are required before changing that range or the
  median-filter width.
- **Chords and double-stops collapse to one note.** A tracker returns one pitch
  per frame by definition. Bleed from another instrument is also resolved to a
  single note rather than ignored.
- **10.9% of graded frames are octave errors** (against 7.3% before). The
  tracker's remaining error mode is picking the wrong partial, not the wrong note
  class — a plausible next improvement, and the reason `oct` is a separate probe
  column rather than folded into `wrong`.
- **Project-local TOML profiles can retune pYIN.** A bass profile may extend
  `bass` or `bass-pyin` to adjust its frequency window, frame settings,
  median filter, note floor, re-articulation sensitivity, and cleanup recipe. Those output-changing values
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

Add `--onset-reference tests/fixtures/bass_7rivers/hand_corrected_notes.json`
to score note *starts* against the hand annotation. That is the only table
re-articulation splitting moves; the frame-level columns are unchanged by it by
construction.

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

## MT3 alternative (opt-in comparison, issue #288)

Status: **shipped as `bass-mt3`, opt-in, no fallback, single-song evidence
only, and measurably noisy relative to the shipped default on this stem**
(2026-08-03, corrected 2026-08-03 — see "The raw octave gap is a known
convention, not leakage" below). `bass-mt3` feeds the raw `bass` stem into the
pinned MT3 backend and retains only its first note-bearing MIDI track,
unmodified by any cleanup stage. It never replaces or changes `bass`'s pYIN
default, and there is no fallback if MT3 is unavailable or fails.

Measured against the same `7Rivers` bass stem, the same CQT reference, and
the same hand-corrected onset annotation used throughout this document:

| Variant | notes | maxpoly | pitch range | hit% | oct% | wrong% | miss% | prec | rec | f |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `default-bass` (pYIN) | 268 | 1 | 28–50 | 82.6 | 10.9 | 1.6 | 4.9 | 75.5 | 82.6 | **78.9** |
| `bass-mt3` (raw) | 131 | 2 | 47–58 | 0.0 | 46.5 | 22.8 | 30.8 | 0.0 | 0.0 | **0.0** |
| `bass-mt3` (−12 semitones) | 131 | 2 | 35–46 | 27.0 | 19.5 | 22.8 | 30.8 | 35.6 | 27.0 | **30.7** |

### The raw octave gap is a known convention, not leakage

`bass-mt3`'s raw output (47–58) sits a clean octave-plus above `default-bass`'s
range (28–50) and the CQT reference's own graded-frame distribution (`p1 29.0,
median 34.0, p99 44.0`). Read on its own, a non-overlapping pitch range looks
like MT3 selected the wrong instrument's track. **It is not**: the maintainer
notes that the *shipped default* itself needs a 12-semitone (one octave)
transposition to sound correct against the bass instrument actually used for
playback — bass guitar is conventionally notated (and, evidently, authored
here) an octave above where it sounds. MT3 has no reason to know or follow
that convention; it emits standard, non-transposed MIDI pitch. Shifting
`bass-mt3`'s output down 12 semitones before re-scoring against the same
reference moves it from a flat 0.0% to a non-trivial **27.0% hit / 30.7% F**,
confirming the two are working in different octave conventions rather than
different instruments.

That correction narrows the finding, it doesn't erase it: even octave-aligned,
`bass-mt3` still recovers only 27.0% of graded frames correctly (`hit`) against
`default-bass`'s 82.6%, with 22.8% flatly wrong pitches and 30.8% missed —
both **unchanged by the shift**, since a uniform transposition cannot fix a
frame that was never voiced or that landed on the wrong note *within* the
correct octave. Onset timing alone (ignoring pitch, 272-note hand annotation,
50 ms tolerance) is unaffected by any pitch shift: `bass-mt3` reaches 79.4%
onset precision but only 38.2% recall (F 51.6%, against pYIN's 76.1%/75.0%/
**75.6%**) — plausible timing on the notes it does emit, well under half the
onsets found at all.

This is **one song, one run, no fallback** — read it as a relative signal on
`7Rivers`, not an absolute accuracy claim, and the corrected numbers above are
the ones to trust over the raw table. `bass-mt3`'s pitch register is not
evidence against it; its frame-level agreement, even octave-corrected, is
still clearly behind the shipped pYIN default on this stem.

### Track selection was verified correct, so this is a content problem, not a leakage one

Re-running `mt3-transcribe` directly on this same bass stem (outside vgt,
keeping MT3's full multi-track output rather than letting normalization
discard it) shows exactly which instrument landed first: `programs: [0, 32,
47]` (Piano, **Acoustic Bass**, Timpani). The track vgt selected is the one
labeled **"Acoustic Bass" (program 32), 131 notes, first onset at tick 35** —
matching `bass-mt3`'s reported note count exactly. The spurious piano and
timpani tracks start at ticks 4765 and 10916 respectively, far later. So
MT3 did not put the wrong instrument first here; the octave gap above and the
27.0%-hit accuracy gap are both about the *quality* of MT3's bass
transcription, not about which track got selected. See finding 7 in
[instrument-transcription-findings.md](instrument-transcription-findings.md)
for the verified track-selection mechanism this confirms.

### Ear-verified 2026-08-03: confirms the measurement, on a second song too

The maintainer listened to `bass-mt3`'s output end to end in REAPER on both
`7Rivers` and a second song ("Chcemy Bys Soba" — 72 notes, pitch range 28–43,
notably *not* needing the octave correction `7Rivers` did, and no probe scores
computed for it): **not impressed — the current pYIN default gives a better
outcome by ear**, on both songs. This corroborates the frame-level numbers
above rather than complicating them: even where the octave lands correctly
without adjustment (the second song), the actual note content is still
judged worse than the shipped default by ear. `bass-mt3` is **not recommended
over the current bass default** on either song measured so far.

### Reproducing

```sh
vgt transcription backend provision mt3
vgt transcription variant add bass --name mt3 --profile bass-mt3 "Song.RPP"
uv run python scripts/bass_transcription_probe.py \
  /path/to/project/vgt/<namespace>/transcription/bass/<default-id>.csv \
  /path/to/project/vgt/<namespace>/transcription/bass/<mt3-id>.csv \
  --stem /path/to/project/vgt/<namespace>/stems/bass.wav \
  --reference cqt --cache /tmp/bass-ref.npz --agreement \
  --onset-reference tests/fixtures/bass_7rivers/hand_corrected_notes.json
```
