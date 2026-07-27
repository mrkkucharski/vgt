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

### 3. The drum MIDI declares DrumScript's half tempo (60 BPM) — but this does NOT compress playback; it is a MIDI-editor-grid issue at most

**This section has been corrected twice. Final position, grounded in the
maintainer's REAPER observation:** there is **no ~2× time compression** of the
drum MIDI. An intermediate draft claimed one; that was wrong.

Facts that are certain:

- DrumScript reported `backend_tempo = 60.1` (half of 120.004) and both drum
  `.mid` artifacts declare **60.09 BPM**. vgt authors the drum MIDI at that
  tempo (`_write_midi(..., _midi_tempo_bpm(midi_source))` for `drums-clean`; a
  byte-copy of DrumScript's 60 BPM file for `default`), whereas every other
  target authors at the project's tempo via `spec.midi_tempo`. So the drum
  MIDI's *declared* tempo differs from the project's.

What the maintainer confirmed in REAPER, which settles the consequence:

- The vgt-imported drum MIDI **spans the full song and stays aligned with the
  drum stem** — it does not finish at the midpoint. REAPER imports these items
  time-based (`BEAT 0`) and preserves each note's absolute time, so the 60-BPM
  declaration does **not** re-time playback. Playback timing is therefore
  **not** compressed by the tempo label.
- My earlier "2× compression" reasoning was an artifact of two mistakes: (a) I
  converted the RPP's PPQ ticks to seconds using the *project* 120 BPM for a
  *time-based* item, which halved the numbers in my parse but not in REAPER's
  playback; and (b) I compared a `[work]` MIDI copy — which the maintainer had
  **trimmed to measures 3–30 (~57 s)** — against the *full-song* raw detection
  (`drums.json`, 0–160 s), making a trimmed excerpt look "packed."

So the tempo label is **not** a playback-timing cause. Its only real effect is
that the MIDI editor's bar/beat ruler (and any quantize/notation) reads at half
tempo — a nuisance for editing, not for listening. For a practice *reference*
that is played, not quantized, this is low value.

The real timing complaint ("notes not on the beat") is the **local per-onset
error** (cause #1's broken alignment plus DrumScript's own onset jitter),
addressed by fixing the evidence normalization and then snapping onsets to the
grid — not by touching the tempo. Note also that DrumScript has **no
tempo-input flag** (`main.py` auto-detects; only `--ts` exists, for the PDF
score), so there is no backend lever here regardless.

Open confirmation (only the maintainer can answer): do the notes sit *slightly*
off the beat consistently across the whole 57 s (a fixed local offset), or do
they progressively slide relative to the audio (which would reopen a genuine
rate/tempo problem)? Everything above assumes the former.

### 4. DrumScript over-detects and hallucinates in quiet passages

Independently of cleanup, `default` emits ~2× the real note count, driven by
repeated hi-hat retriggers and kick doubles, plus notes in sections that are
actually silent. This is a backend-quality ceiling, not something the
current conservative cleanup is allowed to touch (it only removes on strong
negative evidence, which — per cause #2 — it can't measure).

## Direct answers to the questions asked

- **Why is timing off when the click/beats align with the drums?** It is a
  **local per-onset error**, not a global tempo/compression problem (the
  maintainer confirmed the drum MIDI spans the full song and stays aligned — see
  cause #3). The beat grid and the drum onsets come from *different* estimators
  (vgt's accurate, audio-aligned grid vs DrumScript's own onset detector, which
  is jittery and sits tens of ms off the grid), and vgt never reconciles them.
  `drums-clean` was meant to nudge onsets to audio peaks, but its evidence signal
  is broken (cause #2), so for 73% of notes it does nothing — leaving DrumScript's
  raw, off-grid placement in place.

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

3. **(Optional, low priority) Relabel the drum MIDI's tempo to the project's.**
   The drum MIDI declares DrumScript's 60 BPM while the project is 120. Per the
   maintainer this does **not** compress playback (the item is imported
   time-based and stays aligned), so this is *not* a timing fix — it only makes
   the MIDI editor's bar/beat ruler and quantize read correctly. Carry
   `spec.midi_tempo` into `DrumScriptSpec` and pass it to `_write_midi` (as
   `BasicPitchTranscriber` already does), rescaling ticks so absolute note times
   are preserved. Do only if the half-tempo editor grid becomes a practical
   annoyance; it does not affect the reference when played. **Deferred pending
   the maintainer's local-offset-vs-drift confirmation in cause #3** — if the
   notes turn out to *drift* across the song, this becomes a real rate fix and
   gets re-prioritized.

4. **Tame over-detection before suppression.** A conservative de-dup of
   near-coincident same-instrument retriggers (a minimum inter-onset interval
   per instrument, tempo-scaled) would cut the ~2× hi-hat/kick inflation
   without needing per-note audio evidence — complementary to, and safer
   than, evidence-based suppression.

5. **Make the `[work]` corrected MIDI a first-class evaluation fixture.**
   *Delivered:* the offline scorer shipped in #182 (`scripts/drum_midi_score.py`),
   and the human-corrected reference is committed at
   `tests/fixtures/drums_7rivers/`. **Important:** that ground truth covers only
   **measures 3–30 (~0–57 s)** — the maintainer cleaned and trimmed just that
   span — so any scoring against it must restrict the candidate to the same
   window, or the full-song candidate's later notes read as false positives.

Ordering rationale (revised): **#1 (evidence normalization) is the top real
timing/notes win** — it restores the suppressed notes and lets alignment run.
#5 (the scorer) is the delivered measurement backbone. #2 then moves the residual
per-onset error onto the beat; #4 removes the over-detection. #3 (tempo relabel)
is **demoted to optional/low** and deferred pending the maintainer's
local-offset-vs-drift confirmation — it is not a playback-timing fix. The GitHub
issues were re-ordered accordingly (the tempo issue is on hold, not high
priority).
