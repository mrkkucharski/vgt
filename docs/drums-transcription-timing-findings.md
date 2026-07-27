# Drums transcription: timing & density findings (7Rivers)

Status: diagnostic evidence for why the shipped drums reference MIDI is
mistimed and has wrong note counts. Measured against the real `7Rivers`
project on 2026-07-27. This document does not change any code; it records
what was done, what the outcomes actually are, the root causes, and a
prioritized improvement plan.

## What was compared

Four tracks in the project's `[work]` folder, all sourced from the same
LALAL drum stem (`vgt/6a7745be/stems/drums.wav`, item placed at project
4.000s, no source offset):

| Track | What it is | Note-ons |
|---|---|---:|
| `[work] Drums` | the drum stem audio (reference truth) | — |
| `[work] Drums MIDI corrected` | human-edited MIDI (ground truth) | 323 |
| `[work] Drums Ref (MIDI)` | DrumScript `default` profile (raw) | 659 |
| `[work] Drums Ref — drums-clean (MIDI)` | DrumScript + `drums-clean` (latest attempt) | 329 |

Project grid: **120.004 BPM**, beat period 0.49998s, downbeat offset
0.0853s. The stem audio and the beat/click grid line up in REAPER — that
part is correct and is *not* where the problem is.

## Headline measurements

**Timing.** Distance of each note to the nearest metric-grid line
(smaller = tighter to the beat):

| | vs 8th-note grid (median \|offset\|) |
|---|---:|
| Human corrected (natural feel) | **15 ms** |
| DrumScript `default` | **40 ms** |
| `drums-clean` (latest) | **50 ms** |

DrumScript's raw onsets are a **median +45 ms late** vs the real hits, and
`drums-clean` made timing *worse*, not better. Only **47%** of the real
(human) notes have a `default` note within 50 ms; for `drums-clean` it is
only **20%**.

**Density (notes per 2 bars, human truth ≈ 16):**

- `default` runs **~2× too many** almost everywhere (34–55 vs 16), because
  DrumScript over-detects hi-hats (348) and kicks (209). In a real drum
  *break* around bars 29–30 (human = 0 notes) it still emits ~32 notes —
  pure bleed/hallucination.
- `drums-clean` is **erratic**: it deletes down to 4–9 notes in some bars
  (missing) and leaves 49–56 in others (too many). This is the user-visible
  "first measures missing, 17–21 missing, then too many notes."

## Root causes

### 1. Two independent timelines; vgt's good grid is never applied to the drum MIDI

vgt already computes an accurate 120 BPM beat grid (the analysis path) that
lines up with the audio. **DrumScript never sees that grid.** It runs its
own onset detection and its own internal beat tracker on the stem, emits
absolute-second onset times, and vgt imports those times as-is. Nothing in
the pipeline snaps drum onsets to vgt's beat grid or to the audio. So the
click can be perfectly aligned while the MIDI is 40–50 ms off — they come
from two unrelated estimators.

### 2. The `drums-clean` audio evidence is mis-normalized — this is the biggest, most fixable bug

`AudioOnsetEvidenceSource.evidence_near` scores every onset as
`local_peak / global_envelope_max` (`src/vgt/drum_cleanup.py:236`).
Reproduced on the real stem:

- global envelope max = **24.6**, but the median frame is **0.09** and even
  the 99th percentile is 16.8 — a handful of loud transients (crashes/fills)
  dominate the maximum.
- Dividing every hit by that global max gives a **median event strength of
  0.027**. Result:
  - **62%** of events fall below the suppression bar
    (`CLEAN_SUPPRESSION_STRENGTH_THRESHOLD = 0.12`) → deleted as
    "weak-local-audio-evidence." In this project **330 / 659 (50%)** of
    events were suppressed, including obvious real hits. → **missing notes.**
  - **73%** fall below the alignment bar
    (`CLEAN_MIN_EVIDENCE_STRENGTH = 0.35`) → the timing-alignment stage never
    fires, so those notes keep DrumScript's raw (late/jittery) time. →
    **timing never actually corrected.**

The strength number is meaningless as an absolute quantity: it measures "how
loud is this hit compared to the single loudest transient in the whole song,"
not "is there an onset here." A quiet-but-real hi-hat and true silence both
score ≈ 0. So the suppression stage cannot tell a real quiet hit from a
non-event, and it removes both. Merely re-normalizing by the 95th percentile
instead of the max already cuts the sub-0.12 fraction from 62% to 41% — and a
proper local/relative prominence measure would do much better.

### 3. The drum MIDI is authored at DrumScript's mis-detected half tempo, not the project tempo — likely the dominant timing bug

**Correction to an earlier draft of this document, which wrongly called the
half-tempo "cosmetic."** It is not.

DrumScript reported `backend_tempo = 60.1` (half of the real 120.004) and both
drum `.mid` artifacts declare **60.09 BPM**. The mechanism that makes this
matter:

- A Standard MIDI File stores note positions as **PPQ ticks against a declared
  tempo**, not as absolute seconds. When such a file is imported into a project
  at a *different* tempo, the ticks are re-timed by the project tempo.
- vgt authors the drum MIDI at DrumScript's tempo:
  `_write_midi(..., tempo_bpm)` with `tempo_bpm = _midi_tempo_bpm(midi_source)`
  (= 60.09) for `drums-clean`, and a raw byte-copy of DrumScript's 60 BPM file
  for `default`. **Every other target** authors at the project's detected tempo
  — `BasicPitchTranscriber` does `_write_midi(..., spec.midi_tempo)` (120.004)
  and even passes `--midi-tempo` to Basic Pitch. `DrumScriptSpec` does not carry
  the project tempo at all; it is dropped. So drums is the **only** target whose
  MIDI tempo disagrees with the project.
- Consequence at 60-authored-vs-120-project: the tick grid is played twice as
  fast, i.e. a **~2× time compression** that grows across the song. The RPP
  bears this out: all 659 detected onsets — which span **0–160 s** in
  DrumScript's true detection seconds (`drums.json`) — are packed into the
  **0–80 s** region when the imported item is read at the project's 120 BPM, and
  six consecutive raw onsets map exactly to `t → t/2` (0.749→0.375, 0.998→0.5,
  1.498→0.75, …). A per-note ±30 ms alignment nudge is meaningless underneath a
  2× compression.

Caveat: REAPER MIDI items are time-based (`BEAT 0`) here, and the precise
playback timing of a time-based item depends on REAPER internals that cannot be
read with certainty from the RPP text alone. The **code inconsistency is
certain** (drums alone is authored at the wrong tempo); the exact in-REAPER
playback consequence should be confirmed by the maintainer (does the drum MIDI
performance finish around the song's midpoint and drift progressively?).

Note on DrumScript's CLI: it has **no tempo-input flag** (`main.py` accepts only
`input_audio_path`, `--full-song`, `--drumless`, `--mute`, `--all-stems`,
`--format`, `--rudiment`, `--ts`; tempo is always auto-detected). So the fix is
**not** to hint DrumScript — it is for vgt to author/relabel the drum MIDI at
the project's own detected tempo (`spec.midi_tempo`), exactly as it already does
for every other instrument.

### 4. DrumScript over-detects and hallucinates in quiet passages

Independently of cleanup, `default` emits ~2× the real note count, driven by
repeated hi-hat retriggers and kick doubles, plus notes in sections that are
actually silent. This is a backend-quality ceiling, not something the
current conservative cleanup is allowed to touch (it only removes on strong
negative evidence, which — per cause #2 — it can't measure).

## Direct answers to the questions asked

- **Why is timing off when the click/beats align with the drums?** Two layers.
  The dominant one is the **tempo mismatch (cause #3)**: the drum MIDI is
  authored at DrumScript's mis-detected 60 BPM while the project is 120, so on
  import the whole performance is time-compressed (~2×) relative to the audio —
  progressively worse across the song. Underneath that, even at the right tempo
  the beat grid and the drum onsets come from *different* estimators (vgt's
  accurate grid vs DrumScript's own detector, ~+45 ms late and jittery) that vgt
  never reconciles; `drums-clean` was meant to nudge onsets to audio peaks but
  its evidence signal is broken (cause #2), so for 73% of notes it does nothing.
  Fix the tempo first, then the residual per-onset error.

- **Why too many notes in some places and missing notes in others (latest
  attempt)?** Two stacked effects. DrumScript over-detects to begin with
  (~2×, cause #4). Then `drums-clean`'s suppression, driven by the
  mis-normalized evidence (cause #2), deletes ~50% of events by a threshold
  that has no stable relationship to whether a hit is real — so wherever the
  local audio happens to sit below the global-max-normalized bar it strips
  the bar down to a few notes (the "missing measures"), and wherever a loud
  passage clears the bar it keeps DrumScript's full over-detected cluster
  (the "too many notes"). The boundary between the two is an artifact of loud
  vs quiet passages, not of musical correctness.

## Planned improvements (prioritized)

1. **Fix the onset-evidence normalization (highest impact, smallest change).**
   Replace global-max normalization with a *local, relative* prominence:
   e.g. peak height relative to a rolling local median/baseline of the
   envelope, or a percentile-scaled (95th–99th) normalization, so "is there
   an onset here" no longer depends on the loudest crash in the song.
   Re-tune `CLEAN_SUPPRESSION_STRENGTH_THRESHOLD` / `CLEAN_MIN_EVIDENCE_STRENGTH`
   against the human-corrected `[work]` MIDI as a labeled reference. Target:
   suppression removes only genuine non-events; alignment fires on the
   majority of real hits. Every constant is already part of the profile
   identity hash, so retuning is cache-safe and leaves `default` untouched.

2. **Snap onsets to vgt's own beat grid / audio peaks, not just a ±30 ms
   nudge.** Feed the analysis-stage beat grid (and downbeat offset) into
   cleanup so alignment has a musical reference, and widen/condition the
   search so a systematic offset (here ~+45 ms) is corrected rather than
   clipped at the ±30 ms clamp. Consider a measured per-section static offset
   (the profile already supports `CLEAN_STATIC_OFFSET_S`) informed by the
   grid rather than left at 0.

3. **Author the drum MIDI at the project's detected tempo (high priority, likely
   the biggest timing win).** Carry `spec.midi_tempo` (the project's 120.004)
   into `DrumScriptSpec` and use it in `_write_midi` for both `default` and
   `drums-clean`, instead of DrumScript's mis-detected 60 BPM — matching what
   `BasicPitchTranscriber` already does for every other target. DrumScript has
   no tempo-input flag, so this is done on vgt's side by relabeling/authoring
   the derived MIDI, not by asking the backend. This removes the ~2× compression
   at its source and must land **before** the ±30 ms grid-alignment work (#2),
   which is meaningless underneath a tempo mismatch. Confirm the fix in REAPER:
   the drum MIDI performance should span the whole song and stay with the audio
   instead of finishing near the midpoint.

4. **Tame over-detection before suppression.** A conservative de-dup of
   near-coincident same-instrument retriggers (a minimum inter-onset interval
   per instrument, tempo-scaled) would cut the ~2× hi-hat/kick inflation
   without needing per-note audio evidence — complementary to, and safer
   than, evidence-based suppression.

5. **Make the `[work]` corrected MIDI a first-class evaluation fixture.**
   The human-corrected track is a ready-made ground truth. A small offline
   scorer (precision/recall/median-timing-error vs the corrected MIDI) would
   turn every future cleanup change into a measured before/after instead of a
   listening guess, and would have caught the "drums-clean made timing worse"
   regression automatically.

Ordering rationale (revised): **#3 (tempo authoring) should land first** — it is
the largest and simplest timing win and everything else is measured on top of it
(a ±30 ms nudge is meaningless while the track is 2× compressed). #5 (the scorer,
already delivered in #182) is the measurement backbone. #1 restores the missing
notes and lets alignment run; #2 then moves the residual onset error onto the
beat; #4 removes the over-detection. The GitHub issue priorities were set before
this tempo finding and should be re-ordered to put the tempo fix ahead of the
±30 ms alignment work.
