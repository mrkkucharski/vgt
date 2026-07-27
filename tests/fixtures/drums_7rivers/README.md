# 7Rivers drum-cleanup fixtures

Offline ground truth for the drum-transcription cleanup work (issues #182–#185,
see `docs/drums-transcription-timing-findings.md`). The source REAPER project
lives only on the maintainer's host and is **not** in this repo; these small
derived files let the work be built and regression-tested with no REAPER, no
network, no model, and no audio — the same bar as the rest of the `Tests`
workflow.

All times are **seconds relative to the drum stem's start** (the DrumScript time
origin), so `corrected_ground_truth.json` and `drumscript_raw_events.json` are
directly comparable.

## Files

- **`corrected_ground_truth.json`** — the maintainer's hand-corrected drum MIDI
  (`[work] Drums MIDI corrected`), parsed from the RPP. `[{time_sec, instruments:[...]}]`,
  318 notes. This is the reference truth for scoring precision/recall/timing.
  It is a human creative edit; it contains no audio.
  **Coverage: measures 3-30 only (~0-57 s).** The maintainer cleaned and trimmed
  just that span, so `drumscript_raw_events.json` (full song, 0-160 s) has many
  events with no counterpart here. When scoring, restrict the candidate to the
  0-57 s window (or trim it to this ground truth's span) - otherwise the
  candidate's later notes all count as false positives and the numbers are
  meaningless.

- **`drumscript_raw_events.json`** — DrumScript 0.1.6's raw `default`-profile
  output for the same stem. `[{time_sec, instruments:[...]}]`, 421 events. The
  input a cleanup pass receives.

- **`onset_strength_envelope.json`** — a librosa `onset_strength` envelope of the
  drum stem: a 1-D array of frame strengths plus `sr`, `hop_length`,
  `frame_rate_hz`, and the precomputed `global_max`/`median`. **Numbers only —
  not reconstructable to audio.** It reproduces the real "a few loud transients
  dominate the global maximum" pathology (global_max ≈ 24.6 vs median ≈ 0.09)
  that breaks `drums-clean`'s evidence normalization. Feed it through an
  evidence source (analogous to `TableOnsetEvidenceSource`) to test the fix
  without decoding audio.

## Provenance & scope

Derived from the maintainer's own recording ("The Seven Rivers"). Committed as
numeric/symbolic data only, with the maintainer's consent, specifically so the
remote implementer can verify offline. Do **not** add the audio stems, the full
`.RPP`, or any reconstructable signal here. Real-song listening in REAPER
remains the maintainer's manual, non-blocking check — never an automated
acceptance gate.

Regenerated on the maintainer's host via `scripts/`-style extraction from
`reaper/7Rivers`; if the corrected MIDI changes, re-extract and update these
files in the same shape.
