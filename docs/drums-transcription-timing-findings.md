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

### 3. The drum MIDI is authored at DrumScript's half tempo (60 BPM) — this DOES compress playback ~2× and is the dominant timing bug

**Fixed by issue #193:** `DrumScriptSpec` now carries `midi_tempo` (the
project's detected tempo), and `DrumScriptTranscriber.transcribe` authors
both the `default` and `drums-clean` MIDI at `spec.midi_tempo` instead of
DrumScript's own 60 BPM detection — `default` no longer byte-copies
DrumScript's MIDI; it re-authors from `raw_events`' real-second onsets,
recovering velocity from DrumScript's MIDI since its event JSON doesn't
carry it. The rest of this section is kept as the diagnostic record of the
bug that motivated the fix.

**This section previously claimed the tempo label was harmless. That was
wrong, and this is the corrected, evidence-backed position (re-verified against
the real `7Rivers.RPP` on 2026-07-28).** The 60 BPM declaration causes a
genuine **~2× playback compression**: the drum MIDI plays back at double speed,
finishing at roughly the midpoint of the song, with every note progressively
earlier than the audio it transcribes. This — not per-onset jitter — is why the
`[vgt] Drums Ref — drums-clean (MIDI)` item is visibly **much shorter** than the
`[vgt] Drums` stem and why the notes are off the beat.

**Why it happens (mechanism, fully traced in the RPP):**

- DrumScript's beat tracker made a classic **half-tempo octave error**: it
  reported `backend_tempo ≈ 60.09` for a stem whose true tempo is `120.004`
  (exactly ½). Both drum `.mid` artifacts declare **60.09 BPM**; the sidecar
  records `midi_tempo = 60.09` for every drums variant vs `120.004` for guitar.
- vgt authors the drum MIDI at *that* tempo:
  `_write_midi(..., _midi_tempo_bpm(midi_source) or 120.0)` for `drums-clean`
  (`src/vgt/transcribe.py:2077`, `:2083`), and a **byte-copy** of DrumScript's
  60 BPM file for `default` (`:2073`–`:2074`). Every other target authors at the
  project tempo via `spec.midi_tempo` (`:1210`, `:1246`) — which is exactly why
  guitar is correct and drums are not. `DrumScriptSpec` has **no `midi_tempo`
  field** (`:641`), so the DrumScript path never learns the project's tempo.
- At 60 BPM, "1 beat = 1 second", so a hit at real audio second *T* is written
  at **QN = T** (not `2T`, which is what 120 BPM would give).
- REAPER imports the items to **follow the project tempo map**: every drum MIDI
  item carries `IGNTEMPO 0 120 4 4` (first field `0` = *use project tempo*, not
  the item's stored tempo). So a note stored at QN *Q* plays at
  `item_start + Q × 0.5 s` (0.5 s/QN at 120 BPM). The `BEAT 0` flag governs how
  the item's *position/length* react to tempo changes; it does **not** make the
  note content time-based — the QN content is always mapped through the project
  grid.
- Therefore a drum note authored at QN = *T* plays at `4 + 0.5T`, while its real
  audio hit is at `4 + T`. It fires at **half the elapsed offset** — 2×
  compressed, and the error grows with *T* (a progressive drift, not a fixed
  offset).

**Direct measurement from the RPP (proves it):**

| Track | Authored tempo | Last note-on (QN) | Plays at (proj. 120 BPM) | Real audio time | Aligned? |
|---|---|---:|---:|---:|:--:|
| `[vgt] Guitar Ref — clean` | 120 BPM | 352.35 | 4 + 176.2 = **180 s** | ~180 s | yes |
| `[vgt] Drums Ref — drums-clean` | **60 BPM** | 144.00 | 4 + 72 = **76 s** | 4 + 144 = **148 s** | **no, 2× early** |

The guitar MIDI (authored at 120 BPM → QN = 2T) maps straight back to real
seconds under the 120 grid and spans the full song. The drum MIDI (authored at
60 BPM → QN = T) is squeezed into the first half.

**Correcting the earlier reversal:** the intermediate "no compression"
conclusion rested on a REAPER observation that the drum MIDI "spans the full
song and stays aligned." That observation could not be reproduced from the
committed project — every drum MIDI item ends near QN 144 (~72 s of playback),
well short of the ~178 s stem — and the `IGNTEMPO 0` flag on every item
contradicts the "imported time-based, absolute time preserved" claim. The open
question this section used to pose ("fixed local offset or progressive drift?")
is answered: it is a **progressive rate drift**, i.e. a genuine tempo bug.

DrumScript has **no tempo-input flag** (`main.py` auto-detects; only `--ts`
exists, for the PDF score), but no backend lever is needed: vgt already knows
the correct project tempo and already holds the events in real seconds (the
events JSON, 0–160 s, is correct), so vgt can and should author the MIDI on its
own 120 BPM grid. **This is a vgt-only fix; DrumScript needs no change.**

### 4. DrumScript over-detects and hallucinates in quiet passages

Independently of cleanup, `default` emits ~2× the real note count, driven by
repeated hi-hat retriggers and kick doubles, plus notes in sections that are
actually silent. This is a backend-quality ceiling, not something the
current conservative cleanup is allowed to touch (it only removes on strong
negative evidence, which — per cause #2 — it can't measure).

## Direct answers to the questions asked

- **Why is timing off when the click/beats align with the drums?** There are
  **two** effects, and the larger one is global. (a) **Gross:** the drum MIDI is
  authored at DrumScript's half tempo (60 BPM) and REAPER plays it on the
  project's 120 BPM grid, so it plays back **~2× compressed** and drifts
  progressively earlier than the audio — the dominant "not on the beat" and
  "much shorter" symptom (cause #3). (b) **Residual:** even after the tempo is
  fixed, a **local per-onset error** remains, because the beat grid and the drum
  onsets come from *different* estimators (vgt's accurate, audio-aligned grid vs
  DrumScript's jittery onset detector) and vgt never reconciles them.
  `drums-clean` was meant to nudge onsets to audio peaks, but its evidence signal
  is broken (cause #2), so for 73% of notes it does nothing. Fix (a) first (it
  restores the correct timeline); (b) then moves the residual jitter onto the beat.

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

3. **(HIGHEST priority — gross timing fix) Author the drum MIDI at the project
   tempo, not DrumScript's.** The drum MIDI is authored at DrumScript's 60 BPM
   while the project is 120, and REAPER plays it on the project grid
   (`IGNTEMPO 0`), so it plays **~2× compressed** and drifts (cause #3, now
   confirmed). This is a genuine rate bug, not a cosmetic relabel. Carry
   `spec.midi_tempo` (the project's detected tempo) into `DrumScriptSpec` and use
   it in `_write_midi` for **both** the `drums-clean` branch and the `default`
   branch — the latter must stop byte-copying DrumScript's 60 BPM file and
   re-author from `raw_events` (real-second onsets it already has) at the project
   tempo, exactly as `BasicPitchTranscriber` does. Because the events carry
   correct real seconds, authoring at 120 BPM (QN = 2 × seconds) maps back to
   real seconds under the project grid → full length, on the beat. vgt-only; no
   DrumScript change. Validate that the events JSON carries velocity so the
   re-authored `default` reproduces the same notes, only re-timed.

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

Ordering rationale (revised 2026-07-28): **#3 (author at project tempo) is now
the top priority** — it fixes the gross ~2× playback compression that dominates
the "off the beat / much shorter" symptom and puts every drum note back on the
correct timeline. Only once the timeline is correct do the finer fixes matter:
**#1 (evidence normalization)** restores suppressed notes and lets alignment run,
**#5 (the scorer)** is the delivered measurement backbone, **#2** moves the
residual per-onset jitter onto the beat, and **#4** removes the over-detection.
The earlier ordering demoted #3 as a cosmetic relabel; that was based on an
unreproducible observation and is corrected here.
