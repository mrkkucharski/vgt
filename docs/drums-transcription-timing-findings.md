# Drums transcription: timing & density findings (7Rivers)

Status: historical diagnostic evidence from the real `7Rivers` project
(2026-07-27–28), reconciled with the delivered implementation. The measurements
below preserve the evidence that motivated the fixes, but do not describe the
current shipped timing path.

## Current status (delivered versus remaining)

- **Delivered:** issue #193 authors both drum profiles on the project tempo;
  issue #183 replaced global-max onset evidence with local prominence; issue
  #185 added conservative, tempo-scaled same-instrument retrigger
  deduplication; and the later grid/item fixes (#6 and #7 below) reconcile
  eligible DrumScript events to the analyzed grid and make reference MIDI span
  the source track. These are bounded cleanup and authoring behavior, not a
  claim that transcription is ground truth.
- **Superseded historical measurements:** the original late/onset-strength and
  2×-compression findings record pre-fix behavior. They remain below for
  provenance and are explicitly marked where the old mechanism is discussed.
- **Remaining backend-quality limitation:** DrumScript can still over-detect
  or hallucinate notes, especially in sparse or quiet passages. The cleanup is
  intentionally conservative, so it does not promise to remove every bad
  backend event.
- **Human-owned check:** listening to real stems and inspecting results in live
  REAPER require the maintainer's ears and project; they are non-blocking
  manual checks, not autonomous acceptance criteria.

## Follow-up (2026-08-01): corrected-tempo projects without a beat array

A real `Perfect - Chcemy byc soba` project exposed a second DrumScript timing
case. After the maintainer synchronized a manually corrected REAPER tempo map,
the effective tempo value intentionally no longer contained the detector's old
`beat_times`. DrumScript still returned a uniformly quantized clock (about
232 ms per slot, from its own 64.6 BPM estimate), but vgt had no trusted beat
array with which to reconcile its phase. ADTOF remained aligned because it
emits frame-level audio times rather than DrumScript's internal rhythm grid.

VGT now recognizes that narrow fallback case: only when no project beat grid
is available and the backend timestamps prove uniformly quantized, each
distinct DrumScript slot may move to one strong, unambiguous nearby audio
onset. Unquantized output, weak evidence, ambiguous peaks, and the established
project-grid path remain unchanged. The first implementation used librosa's
default centered onset envelope, however, so the recovered timestamps retained
the STFT half-window latency. On the real stem:

| Evidence timestamps | Median offset from nearby ADTOF events |
| --- | ---: |
| centered onset envelope | **+20.1 ms** |
| uncentered onset envelope | **−3.1 ms** |

The fallback therefore uses `center=False`, and a cleanup profile in that same
no-grid path reuses the uncentered evidence instead of moving corrected events
late again. Cleanup following the normal trusted-grid path deliberately keeps
its historical centered evidence, so the measured 7Rivers behavior is not
silently retuned. The alignment algorithm version is part of the DrumScript
spec identity, so old centered artifacts cannot be reused from cache.

### Potential DrumScript improvements

The VGT correction is sufficient for current projects, but it reconstructs
performed timing after DrumScript has quantized it. Cleaner upstream interfaces
would be:

1. Make rhythmic quantization optional for machine-readable MIDI/JSON output.
2. Emit each classifier onset's pre-quantization timestamp alongside the
   quantized score position and document which one is authoritative for audio
   synchronization.
3. Accept an external tempo/beat grid, or at least a tempo and phase anchor, so
   callers that already analyzed the project do not have to accept a second,
   potentially half-tempo beat estimate.
4. Record the detected grid, phase, subdivision, and every quantization delta
   in JSON. That would make timing errors auditable instead of requiring vgt to
   infer the hidden grid from repeated timestamp differences.
5. Separate onset classification from notation/score formatting so downstream
   applications can consume frame-level detections without importing score
   assumptions.

These changes would improve timing fidelity and observability, but they would
not address DrumScript's separate over-detection and instrument-classification
limitations described below.

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

> **Superseded for the raw-onset number.** These were measured on note
> positions in the RPP while cause #3 (the 60 BPM authoring) was still
> compressing playback ~2×, which mixes the rate bug into the per-onset
> figure. Measured on the event JSON after #193, DrumScript's onsets are
> **early, not late**, and drift — see
> "[Follow-up: the residual offset is DrumScript's own grid](#follow-up-2026-07-28-the-residual-offset-is-drumscripts-own-grid)".

**Density (notes per 2 bars, human truth ≈ 16):**

- `default` runs **~2× too many** almost everywhere (34–55 vs 16), because
  DrumScript over-detects hi-hats (348) and kicks (209). In a real drum
  *break* around bars 29–30 (human = 0 notes) it still emits ~32 notes —
  pure bleed/hallucination.
- `drums-clean` is **erratic**: it deletes down to 4–9 notes in some bars
  (missing) and leaves 49–56 in others (too many). This is the user-visible
  "first measures missing, 17–21 missing, then too many notes."

## Root causes

### 1. Historical: two independent timelines before grid reconciliation

vgt already computes an accurate 120 BPM beat grid (the analysis path) that
lines up with the audio. Before the delivered reconciliation, **DrumScript
never saw that grid.** It runs its
own onset detection and its own internal beat tracker on the stem, emits
absolute-second onset times, and vgt imports those times as-is. Nothing in
the pipeline snaps drum onsets to vgt's beat grid or to the audio. So the
click can be perfectly aligned while the MIDI is 40–50 ms off — they come
from two unrelated estimators.

### 2. Historical: `drums-clean` audio evidence used global-max normalization

Before issue #183, `AudioOnsetEvidenceSource.evidence_near` scored every onset as
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

The old strength number was meaningless as an absolute quantity: it measures "how
loud is this hit compared to the single loudest transient in the whole song,"
not "is there an onset here." A quiet-but-real hi-hat and true silence both
score ≈ 0. So the suppression stage cannot tell a real quiet hit from a
non-event, and it removes both. Merely re-normalizing by the 95th percentile
instead of the max already cuts the sub-0.12 fraction from 62% to 41% — and a
proper local/relative prominence measure would do much better.

**Delivered by #183:** evidence now uses local prominence relative to a rolling
baseline/spread, with thresholds retuned for that scale. The figures in this
section are therefore historical pre-fix measurements, not current behavior.

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
actually silent. This remains a backend-quality ceiling. Issue #185 delivered
conservative, tempo-scaled same-instrument retrigger deduplication, and #183
made negative audio evidence meaningful; neither intentionally turns the
cleanup into an aggressive note-selection model, so false positives can
remain.

## Historical answers to the original questions

- **Why was timing off when the click/beats aligned with the drums?** There were
  **two** effects, and the larger one is global. (a) **Gross:** the drum MIDI is
  authored at DrumScript's half tempo (60 BPM) and REAPER plays it on the
  project's 120 BPM grid, so it plays back **~2× compressed** and drifts
  progressively earlier than the audio — the dominant "not on the beat" and
  "much shorter" symptom (cause #3). (b) **Residual:** even after the tempo is
  fixed, a **local per-onset error** remains, because the beat grid and the drum
  onsets come from *different* estimators (vgt's accurate, audio-aligned grid vs
  DrumScript's jittery onset detector) and vgt never reconciles them.
  `drums-clean` was meant to nudge onsets to audio peaks, but its evidence signal
  then used the broken mechanism in cause #2. The delivered authoring, local
  evidence, and grid fixes supersede this timing diagnosis.

- **Why too many notes in some places and missing notes in others (latest
  attempt)?** Two stacked effects. DrumScript over-detects to begin with
  (~2×, cause #4). Then `drums-clean`'s suppression, driven by the
  then-mis-normalized evidence (cause #2), deleted ~50% of events by a threshold
  that has no stable relationship to whether a hit is real — so wherever the
  local audio happens to sit below the global-max-normalized bar it strips
  the bar down to a few notes (the "missing measures"), and wherever a loud
  passage clears the bar it keeps DrumScript's full over-detected cluster
  (the "too many notes"). The boundary between the two is an artifact of loud
  vs quiet passages, not of musical correctness.

## Historical improvement plan (now delivered where noted)

1. **Delivered by #183 — fix the onset-evidence normalization.**
   Replace global-max normalization with a *local, relative* prominence:
   e.g. peak height relative to a rolling local median/baseline of the
   envelope, or a percentile-scaled (95th–99th) normalization, so "is there
   an onset here" no longer depends on the loudest crash in the song.
   Re-tune `CLEAN_SUPPRESSION_STRENGTH_THRESHOLD` / `CLEAN_MIN_EVIDENCE_STRENGTH`
   against the human-corrected `[work]` MIDI as a labeled reference. Target:
   suppression removes only genuine non-events; alignment fires on the
   majority of real hits. Every constant is already part of the profile
   identity hash, so retuning is cache-safe and leaves `default` untouched.

2. **Delivered by the grid reconciliation — snap onsets to vgt's own beat grid / audio peaks, not just a ±30 ms
   nudge.** Feed the analysis-stage beat grid (and downbeat offset) into
   cleanup so alignment has a musical reference, and widen/condition the
   search so a systematic offset (here ~+45 ms) is corrected rather than
   clipped at the ±30 ms clamp. Consider a measured per-section static offset
   (the profile already supports `CLEAN_STATIC_OFFSET_S`) informed by the
   grid rather than left at 0.

3. **Delivered by #193 — author the drum MIDI at the project
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

4. **Delivered by #185 — tame over-detection before suppression.** A conservative de-dup of
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

Ordering rationale (revised again 2026-07-28, after the follow-up below):
**#3 and the grid reconciliation in cause #6 are delivered, and timing is no
longer the bottleneck** — the reference MIDI now sits within ~17 ms of the
played hits for the whole song. Item #2 as written (widen the ±30 ms nudge to
correct a systematic offset) is **obsolete**: there is no systematic offset
left to correct, and `drums-clean`'s alignment stage now receives events that
are already on the grid. The normalization and conservative retrigger-
deduplication work in #1 and #4 are now delivered. What remains is the bounded
backend-quality limitation: DrumScript can still emit too many notes, so a
passage can read as wrong even when the notes it emits are on the beat. Assess
real-audio listening in REAPER as a human-owned, non-blocking check rather
than an automated acceptance gate.

## Follow-up (2026-07-28): the residual offset is DrumScript's own grid

Measured after #193 landed, on the same project. Two separate defects were
left, one in the transcription and one in the REAPER placement; both are
fixed on this branch.

### Cause #6: DrumScript quantizes onto its own zero-anchored grid

Read straight out of the artifact (`vgt/<ns>/transcription/drums/*.json`),
with no REAPER involved:

| | value |
|---|---|
| Project grid (analysis) | 120.004 BPM, downbeat **0.085333 s**, eighth 0.249992 s |
| DrumScript event grid | anchored at **exactly 0.0**, step **0.249615 s** (= 120.185 BPM) |
| First real drum hit (stem audio, and the corrected MIDI) | **0.0727 s** |
| First event DrumScript reports | **0.0** |

All 410 distinct event times in the `default` variant are multiples of
0.249615 s to within 1.4 ms: DrumScript does not emit the onsets it detected,
it emits the grid it fitted to them. The error against the performance is
therefore `≈ 85 ms + 0.15% × t` — the notes start at the item edge instead of
the first beat, and by t = 160 s they are ~0.33 s (more than one eighth)
ahead. Late in the song this *looks* close to the grid again only because the
error has wrapped past a whole subdivision.

`drums-clean` could not fix this: `_systematic_audio_offset` estimates one
constant latency, and a rate error is not constant. That is why `default`
sounded better than `drums-clean`.

**Fix:** `vgt.drum_grid` moves each event to the nearest line of the analyzed
grid, at the subdivision the backend was using, before either profile runs.
Counting subdivisions instead -- re-emitting the backend's own index -- is
tempting and wrong: the two grids disagree about how many subdivisions have
elapsed, so past the point where that disagreement exceeds half a slot every
note lands one eighth out (visible on 7Rivers from bar 18). Against the stem's
onsets, index mapping leaves 92 of 410 events more than 60 ms from any real
hit; snapping leaves 6.

For a constant grid the target lines come from the *fitted* tempo anchored at
the analyzed downbeat, not from the raw `beat_times` array:
individual detected beats are noisy (eight of 7Rivers' 355 sit more than 50 ms
off the fitted line, one by 140 ms), and authoring onto the array copies each
of those local errors into the MIDI -- the notes then follow the beat
tracker's hiccup instead of the drummer. Measured against stem onsets, the
fitted line has 2 beats >50 ms out where the raw array has 8. A piecewise
grid, where the variation is the intended timeline, still subdivides the
measured beats. `beat_grid` is now part of every drums spec's identity, so
re-analysing the tempo invalidates MIDI authored against the old grid. Index
mapping rather than nearest-line snapping is what keeps the late notes right
once drift exceeds half a subdivision. Every precondition is guarded (an
unquantized backend, a grid that starts mid-song, a disagreeing subdivision,
or a correction that varies by more than a beat all leave the events alone),
so a future backend that emits true onsets is unaffected.

Scored with `vgt.drum_midi_score` against `tests/fixtures/drums_7rivers/`,
using the project's real (jittery) `beat_times`:

| | median timing error | notes >60 ms from any stem onset |
|---|---|---|
| as shipped | −88.6 ms | 196 / 410 |
| index-mapped (rejected) | +12.8 ms | 92 / 410 |
| snapped to the fitted grid | **+16.8 ms** | **6 / 410** |

### Cause #7: the reference MIDI item never spanned the source track

`add_reference_midi_variant` set the item's `D_LENGTH` from
`GetMediaSourceLength(source)`. For a **MIDI** source that value is in
*quarter notes*, not the seconds it returns for a WAV, and it stops at the
file's end-of-track marker (right after the last note) rather than at the end
of the song. In the real project the three `[vgt] Guitar Ref` items were
`LENGTH 355` — 355 QN read as 355 seconds, twice the 178.55 s song — and
since items carry `LOOP 1`, the guitar reference silently repeated. The drums
item, whose transcription ends earlier, stopped short instead.

**Fix:** the item spans the reference track (`reference_end - reference_start`,
like `[vgt] Key` and the stems) with `B_LOOPSRC = 0`, so a transcription that
ends before the song stays short instead of repeating.
