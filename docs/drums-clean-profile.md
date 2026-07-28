# `drums-clean`: opt-in conservative cleanup for DrumScript output (issue #177)

`default` and `drums-clean` are peers, not a before/after pair. `default` is
DrumScript's raw output — unfiltered, unaligned, and unreclassified by this
issue, and still what every project gets unless `drums-clean` is explicitly
selected. (Its MIDI is no longer a byte-copy of DrumScript's own file: issue
#193 re-authors it at the project tempo rather than DrumScript's self-detected
tempo, recovering each note's velocity from DrumScript's MIDI in the process.
The event data itself — which onsets, which instruments — is untouched.)
`drums-clean` applies `vgt.drum_cleanup`'s conservative,
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
(see `vgt.transcribe.default_spec_for_target`'s `drumscript` branch). Five
ordered, deterministic stages, each falling back to the untouched raw event
when its own evidence is absent or ambiguous:

1. **Tempo-scaled same-instrument de-duplication**: each DrumScript label
   has an explicit minimum inter-onset interval in
   `CLEAN_DEDUP_MINIMUM_INTER_ONSET_BEATS` (normally 1/32 beat; crash is 1/64
   beat). The local enclosing beat-grid interval converts it to seconds, so
   this is not a fixed millisecond gate. Only the later onset of a clearly
   too-close *same-label* pair is removed; coincident kick/hat/snare events
   are independent. Missing or incomplete beat-grid tempo is ambiguous and
   retains both events.
2. **Coalesce simultaneous events** (`CLEAN_SIMULTANEITY_WINDOW_S = 0.008s`,
   8ms): raw events within this window of the group's earliest member are
   merged into one aligned onset time. Generous relative to the ~2ms
   one-tick drift the observed evidence measured at 120 BPM/480 PPQN, still
   well under a 16th note at any reasonable tempo.
3. **Grid-guided, audio-aware timing alignment**: when the analysis stage
   supplies its detected beat grid and downbeat anchor, cleanup first looks
   up strong audio peaks in a wider `CLEAN_SYSTEMATIC_ALIGNMENT_WINDOW_S =
   0.08s` window. Only peaks within `CLEAN_GRID_REFERENCE_WINDOW_S = 0.08s`
   of an eighth-note grid line contribute; their median raw-to-audio
   difference is a song-specific systematic latency correction. That moves a
   consistent detector lag beyond ±30ms without treating a fill as a beat.
   Cleanup then does its normal `CLEAN_ALIGNMENT_WINDOW_S = 0.03s` (±30ms)
   local search around the corrected time and moves only to a strong,
   unambiguous audio peak (`CLEAN_MIN_EVIDENCE_STRENGTH = 0.30`). It never
   writes a note directly to the grid, so the peak retains natural feel; with
   no usable grid/evidence, this stage safely falls back to the prior local
   behavior. The result is source-start safe. An optional static offset
   (`CLEAN_STATIC_OFFSET_S`) is applied before estimation and remains part of
   the profile identity for a separately measured stable latency.
4. **Velocity shaping**: when local evidence is available and confident (per
   stage 3's threshold), velocity is linearly mapped from evidence strength
   into `[CLEAN_VELOCITY_FLOOR, CLEAN_VELOCITY_CEILING] = [30, 120]`.
   Otherwise, a bounded, role-aware default (`CLEAN_VELOCITY_DEFAULTS`, one
   entry per DrumScript instrument label) is used instead of a flat
   constant.
5. **Conservative suppression**: an event is dropped from the derived MIDI
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
`drums-clean`), so `settings_hash` for `default` is unaffected by this issue.
(`default`'s MIDI output changed separately, under issue #193, to stop
byte-copying DrumScript's file and fix its timeline authoring — see that
issue and the note above.)
