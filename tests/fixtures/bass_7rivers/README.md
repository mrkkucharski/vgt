# 7Rivers bass re-articulation fixtures

Offline ground truth for bass note *onsets* — the failure mode
`docs/bass-transcription-findings.md` calls "re-articulation", where a pitch
tracker emits one held note for a string that was plucked several times. The
source REAPER project lives only on the maintainer's host and is **not** in this
repo; these small derived files let the split be built and regression-tested
with no REAPER, no network, no model, and no audio.

All times are **seconds relative to the bass stem's start**. The stem begins at
project time 4.0 s, which is already subtracted. `stem_offset_s` inside each
file is the further offset from the stem start to the first frame the fixture
covers, so `stem_offset_s + time` is directly comparable to a transcription CSV.

## Why this fixture exists at all

The bass probe's other metrics are frame-level: they ask which pitch is
sounding at each instant. Playing one fret four times running sounds the same
pitch throughout, so a transcript emitting one held note and one emitting four
score **identically** on every one of them — this is measured, not assumed
(see the findings doc's before/after table: `hit`, `oct`, `prec`, `rec` and `f`
are unchanged to the decimal). No estimator derived from the stem can supply
the missing reference either, because every estimator here is frame-level and
shares the same blind spot. A human annotation was the only way to see the bug,
and is the only way to keep it fixed.

## Files

- **`hand_corrected_notes.json`** — the maintainer's hand-corrected bass MIDI
  (`[clean] Bass Ref — default (MIDI)`), parsed from the RPP. `{window_s,
  stem_offset_s, notes: [[start_s, end_s, pitch_midi, velocity], ...]}`. This is
  the reference truth for onset precision/recall. It is a human creative edit;
  it contains no audio.
  **Coverage: the full 178.6 s track, 272 notes**, reviewed end to end. Earlier
  revisions of this fixture covered only a prefix; `window_s` always states the
  annotated span, and scoring must stay inside it.
  **83% of these notes (225 of 272) sit inside a run of repeated notes on one
  pitch**, which is why this part is the right test for re-articulation.

- **`shipped_notes.json`** — vgt's `bass` output for the same stem *before*
  re-articulation splitting existed (`PYIN_ALGORITHM_VERSION = 1`, 162 notes),
  in the same shape and window. The "before" column, kept so the regression can
  show the gap it closes rather than asserting a bare number.

- **`pyin_frames.json`** — the pYIN pitch track and frame RMS envelope over the
  whole stem: `midi` (fractional MIDI per frame, `null` where unvoiced)
  and `rms`, plus `sample_rate_hz`, `hop_length`, `frame_rate_hz`. **Numbers
  only — not reconstructable to audio.** It is exactly what `track_f0` returns,
  so a test can drive `segment_notes` end to end without librosa, the tracker,
  or the stem.

## Provenance & scope

Derived from the maintainer's own recording ("The Seven Rivers"). Committed as
numeric/symbolic data only, with the maintainer's consent, specifically so the
work can be verified offline. Do **not** add the audio stems, the full `.RPP`,
or any reconstructable signal here. Real-song listening in REAPER remains the
maintainer's manual, non-blocking check — never an automated acceptance gate.

Re-extracting is expected and cheap — take the `[clean] Bass Ref — default
(MIDI)` track from `reaper/7Rivers`, subtract the 4.0 s item position, and
rewrite the note files in the same shape. Select that track **by name**: the
project is edited by hand and track indices move. The tuning in
`docs/bass-transcription-findings.md` should be re-checked, not assumed, if the
annotation is revised.
