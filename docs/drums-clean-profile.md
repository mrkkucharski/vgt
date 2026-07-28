# `drums-clean`: opt-in conservative cleanup for DrumScript output (issue #177)

`default` and `drums-clean` are peers, not a before/after pair. `default` is
DrumScript's raw output, byte-copied straight through — unchanged by this
issue, and still what every project gets unless `drums-clean` is explicitly
selected. `drums-clean` applies `vgt.drum_cleanup`'s conservative,
bounded post-processing pass to the same raw events. Retain both variants
(`vgt transcription variant add drums --profile default` /
`--profile drums-clean`) and compare them for any given song rather than
treating one as strictly better — see the user-facing recommendation in
`docs/USER-MANUAL.md`'s DrumScript section.

## Why this exists

Manual comparison of the 7Rivers drums stem, DrumScript's raw reference
MIDI, and a human-corrected `[work]` copy (measures 3-30, ~120 BPM, 213
edited notes: 54 kicks, 69 snares, 90 closed hi-hats) found the human edit
recovered the audible groove well but left several mechanically fixable
issues on the table:

- **Section-dependent timing error**, not a single constant: the edited
  MIDI ran ~14ms late (median) through measures 3-18, ~2ms late through
  19-25 (already essentially aligned), and ~9ms late through 26-30. A single
  global offset (e.g. a flat -14ms correction) would improve one section
  while over-correcting another.
- **Sub-tick coincident-event drift**: notes intended to be simultaneous
  sometimes differed by one MIDI tick.
- **Non-percussion-channel notes**: 34 closed-hi-hat notes in the edited
  MIDI were on channel 1, not GM channel 10.
- **Flattened dynamics**: all 213 edited notes were velocity 100, erasing a
  clear accent hierarchy (e.g. a quiet ghost snare at the end of the
  groove).
- **Groove-shaped assumptions breaking down**: measures 3-25 mostly used a
  five-onset pattern, but measure 26 was a sparse transition and measures
  27-30 introduced a distinct kick/hi-hat-led pattern that independently
  detected audio onsets confirmed. A role that was correct in one measure
  (e.g. a snare on beat 4.5) was wrong an adjacent measure later (more
  consistent with a hi-hat, or with no onset at all).

None of these are a reason to bake in a 7Rivers-shaped groove template,
a fixed event count, or a copy-from-the-previous-measure heuristic — the
last bullet is direct evidence that would break other songs. `drums-clean`
is built to fix the *mechanical* issues (timing, channel, velocity, weak
duplicate events) while leaving instrument classification and event
presence exactly as DrumScript reported them, unless local evidence for a
specific event says otherwise.

## What `drums-clean` does

Implemented in `src/vgt/drum_cleanup.py`, run from `DrumScriptTranscriber`/
`FakeTranscriber` whenever `DrumScriptSpec.cleanup_profile == "drums-clean"`
(see `vgt.transcribe.default_spec_for_target`'s `drumscript` branch). Four
ordered, deterministic stages, each falling back to the untouched raw event
when its own evidence is absent or ambiguous:

1. **Coalesce simultaneous events** (`CLEAN_SIMULTANEITY_WINDOW_S = 0.008s`,
   8ms): raw events within this window of the group's earliest member are
   merged into one aligned onset time. Generous relative to the ~2ms
   one-tick drift the observed evidence measured at 120 BPM/480 PPQN, still
   well under a 16th note at any reasonable tempo.
2. **Bounded audio-aware timing alignment** (`CLEAN_ALIGNMENT_WINDOW_S =
   0.03s`, ±30ms): looks for a local audio onset-strength peak near each
   group's time. Only moves the event when a peak is found, is unambiguous
   (no comparably strong second peak in the same window), and its strength
   is at least `CLEAN_MIN_EVIDENCE_STRENGTH = 0.30`. The result is clamped
   to stay within the window and never below `time_sec = 0.0` (source-start
   safe). An optional static offset (`CLEAN_STATIC_OFFSET_S`) is applied
   before the search and is part of this profile's identity — it defaults
   to `0.0`, so no implicit universal correction (like the -14ms measured on
   one section of one reference track) is ever applied. A future profile
   could set this explicitly for a project that has its own measured,
   stable offset.
3. **Velocity shaping**: when local evidence is available and confident (per
   stage 2's threshold), velocity is linearly mapped from evidence strength
   into `[CLEAN_VELOCITY_FLOOR, CLEAN_VELOCITY_CEILING] = [30, 120]`.
   Otherwise, a bounded, role-aware default (`CLEAN_VELOCITY_DEFAULTS`, one
   entry per DrumScript instrument label) is used instead of a flat
   constant.
4. **Conservative suppression**: an event is dropped from the derived MIDI
   only when local evidence is available and at or below
   `CLEAN_SUPPRESSION_STRENGTH_THRESHOLD = 0.12` — a much stricter bar than
   the alignment/velocity confidence threshold, so a merely-uncertain event
   is kept, not discarded. A suppressed event stays in the generated JSON
   (`cleanup.suppressed: true`, with `cleanup.suppression_reason`) so the
   decision is inspectable, but never becomes a MIDI note.

Every constant above is part of `DrumCleanupProfile.as_identity()`, which
flows into `DrumScriptSpec.to_dict()` → `spec_hash()` whenever
`cleanup_profile != "default"` — retuning any of them changes
`drums-clean`'s settings hash (and therefore its cache/artifact identity)
without touching `default`'s.

Reclassification (e.g. deciding a note should be a hi-hat instead of a
snare) is deliberately **not** implemented: DrumScript reports one
instrument label per onset with no per-label confidence signal to key a
conservative reclassification on, so `drums-clean` always keeps DrumScript's
classification. This trivially satisfies "any reclassification ... falls
back to the raw event when evidence is ambiguous" — there is no
reclassification path to be non-conservative about.

## Audio evidence sources

`vgt.drum_cleanup` never invokes onset detection directly — every stage
reads through an `OnsetEvidenceSource`:

- `AudioOnsetEvidenceSource` (production): a bounded local search over a
  librosa onset-strength envelope of the drum stem. Never raises — any
  failure to load or analyze the audio (missing file, degenerate/silent
  signal) leaves it reporting "no evidence" for every lookup, which is this
  module's normal, tested no-op path, not a special case. Strength is a
  *local, relative prominence* (issue #183), a classic adaptive onset
  threshold: each frame is compared to its own rolling local median (a
  0.5s-radius baseline) and scaled by a multiple of that same window's local
  median absolute deviation — not a fraction of the single loudest transient
  in the whole file, which used to make every quieter but genuine hit score
  near zero once one loud crash or fill was present, and not a song-wide
  scale, which a dense, evenly-loud passage (e.g. continuous hi-hats) would
  otherwise wash out.
- `TableOnsetEvidenceSource` (tests): deterministic, precomputed
  `(time, strength)` candidates, used to exercise strong, weak, and
  ambiguous evidence without decoding real audio.
- `NullOnsetEvidenceSource`: always "no evidence." Used by `FakeTranscriber`
  offline (there is no real audio to analyze in tests) and available to any
  caller that wants the deliberately safe fallback path.

## Compatibility

`DrumScriptSpec.to_dict()` reproduces the exact pre-#177 five-field shape
whenever `cleanup_profile == "default"` (the implicit selection for every
existing project and every `drums` variant that doesn't explicitly ask for
`drums-clean`), so `settings_hash`, cached artifacts, and MIDI/event output
for `default` are byte-identical to before this issue.
