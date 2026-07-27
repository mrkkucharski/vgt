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

### 3. DrumScript locked onto half tempo (60 BPM) — but this is cosmetic, and cannot be fixed by feeding a tempo in

The backend reported `backend_tempo = 60.1` and both MIDI artifacts declare
**60.09 BPM** (verified in the `.mid` tempo meta-event) against the real
120.004 BPM.

Reading DrumScript 0.1.6's own source settles two things:

- **The half-tempo does not move any note.** `midi_exporter.export_to_midi`
  writes each note with `start = time_sec` (the raw onset seconds from
  `onset_detector`) and only uses `tempo` to set
  `pretty_midi.PrettyMIDI(initial_tempo=tempo)` — i.e. the tempo meta-event.
  Note times are absolute seconds, never quantized to the tempo grid, so a
  wrong tempo only mislabels the item's internal bar/beat ruler; it is not a
  source of the +45 ms onset error or the note-count problems. Even rewriting
  the meta-event to 120 BPM afterward would not shift a single note — it is a
  display-only fix.
- **You cannot hint the tempo into DrumScript.** Its CLI
  (`main.py`) accepts only `input_audio_path`, `--full-song`, `--drumless`,
  `--mute`, `--all-stems`, `--format`, `--rudiment`, and `--ts`. Tempo is
  always `tempo_detector.estimate_tempo(y, sr)` with **no override flag**, and
  `--ts` (time signature) is passed only to the PDF `score_builder`, not to
  onset detection or tempo. So "apply vgt's detected 120 BPM" is not an
  available lever on the pinned backend, and would be cosmetic even if it
  were.

The half-tempo remains a useful *signal* that DrumScript's beat tracker is
weak, but it is not itself a cause of the user-visible symptoms and is the
lowest-value item to chase.

### 4. DrumScript over-detects and hallucinates in quiet passages

Independently of cleanup, `default` emits ~2× the real note count, driven by
repeated hi-hat retriggers and kick doubles, plus notes in sections that are
actually silent. This is a backend-quality ceiling, not something the
current conservative cleanup is allowed to touch (it only removes on strong
negative evidence, which — per cause #2 — it can't measure).

## Direct answers to the questions asked

- **Why is timing off when the click/beats align with the drums?** The beat
  grid and the drum MIDI come from *different* estimators. The grid is vgt's
  (accurate, audio-aligned). The MIDI timing is DrumScript's own onset
  detector (median +45 ms late, jittery, half-tempo beat lock), and vgt never
  reconciles the two. `drums-clean` was meant to reconcile them by nudging
  onsets to audio peaks, but its evidence signal is broken (cause #2), so for
  73% of notes it does nothing.

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

3. **Half-tempo: only a cheap cosmetic fix is available, deprioritize.**
   DrumScript 0.1.6 has no tempo-input flag (confirmed in its source), and its
   MIDI onset times are absolute seconds untouched by tempo, so there is no way
   to "apply the detected tempo" that would move notes. The only option is to
   rewrite the derived MIDI's tempo meta-event to 120 BPM so the item's
   internal bar ruler reads correctly — display-only, does not affect note
   placement. Do this if/when it's cheap; it fixes none of the reported
   symptoms.

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

Ordering rationale: #1 alone should restore most of the missing notes and let
alignment actually run; #2 is what finally moves notes onto the beat; #3 and
#4 remove the remaining structural errors; #5 keeps it from regressing again.
