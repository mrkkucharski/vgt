# Strum detection: integration assessment

An assessment of [`Strumming Detection Reasearch.pdf`](Strumming%20Detection%20Reasearch.pdf)
against what vgt actually is today: what of it fits, what would break, and the
staged path that gets a strumming reference into REAPER without touching any
existing target's output.

Nothing here is implemented. This is a decision document, written before any
issue is filed, in the shape the per-instrument work already uses (see
[instrument-transcription-findings.md](instrument-transcription-findings.md)
for the method it defers to).

## What the document proposes, in one paragraph

Detect strum *events* on the guitar stem with a classical onset function
(spectral flux / complex-domain / HPSS-percussive), prune them to a minimum
inter-onset interval, classify each as a downstroke or an upstroke, snap the
result to the project's tempo grid, and emit it as a MIDI track (one pitch for
down, another for up) or as REAPER markers. It recommends eventually replacing
the detector with a CRNN trained on a custom real+synthetic dataset, citing
~98 % F1 for the ML approach against ~75–80 % for pure onset detection.

## The headline number is not measured on our input

The ~98 % F1 figure is for **pickup signals** — a jack plugged into a guitar,
one instrument, no separation. The same body of work reports roughly 92 % on
real microphone recordings, and the document itself concedes that
"Lalal-separated stems … often smear transients" and lists that as an
unvalidated risk. vgt's input is always a LALAL-separated stem. So the number
that would justify the ML investment has not been measured on anything
resembling our signal, and the gap between 98 % and "unknown on a separated
stem" is the whole question.

This is finding 5 in the shared method restated: the strongest evidence is the
metric you do not have. Here we don't have it for either approach, which means
the first piece of work is measurement, not implementation.

The citations do check out. The current state of the art is
[Joint Transcription of Acoustic Guitar Strumming Directions and Chords](https://arxiv.org/abs/2508.07973)
(Murgul et al., 2025) with its procedural-data companion
[2508.07987](https://www.arxiv.org/pdf/2508.07987); the earlier multimodal
dataset is published at [Klangio/KLANGIO-GST-MM-T](https://github.com/Klangio/KLANGIO-GST-MM-T).
A search surfaced **no publicly released strum-detection weights** — the papers
describe trained models, not downloadable ones. Verify that again before
planning on ML: it is the single fact that decides whether phase 4 below exists
at all.

## Where it fits: the variant mechanism, almost exactly

The good news is that vgt already grew the machinery this needs, for drums.

A strum track is an **onset-event stream authored onto vgt's beat grid** — the
same shape as a drum transcription and nothing like a note transcription. The
pieces that already exist and are tested:

| Need | What already does it |
| --- | --- |
| Onset envelope from a stem | `drum_cleanup.AudioOnsetEvidenceSource` (librosa `onset_strength`; librosa is a hard dependency, so no new install) |
| Event stream → MIDI | `drum_cleanup.cleaned_events_to_midi_notes` |
| Event stream → JSON artifact | `events_artifact_name` / the variant record's `events_file`, `event_count`, `first_event_s`, `last_event_s` |
| A non-note backend behind a profile | `DrumScriptSpec` / `AdtofSpec` and `backend_for_target_profile` — backend is a property of the resolved profile, not of the target |
| Beat grid + downbeat + time signature | `tempo.py`, already threaded into specs for the sustain clamp |
| Chord change times for the doc's "strums coincide with chord changes" constraint | `chords.py` / `chords.txt` |
| Onset P/R/F at ±50 ms with one-to-one matching | `drum_evaluation` (`ONSET_TOLERANCE_SECONDS = 0.050`) |
| Reading a DAW-exported reference MIDI offline | `drum_midi_score.py`'s bundled SMF reader |

And the payoff: **a strum variant needs zero ReaScript changes and zero sidecar
schema changes.** `add_reference_midi_variant` builds a locked track named
`[vgt] Guitar Ref — <label> (MIDI)` from whatever variants the sidecar lists,
so a strum variant of the `guitar` target arrives in REAPER, gets repositioned,
reconciled, discarded, and purged by the code that already ships. The whole
apply/sync surface — the riskiest part of this codebase — is untouched.

## Four things that would break something

### 1. Do not make `strum` a target

`VALID_TARGETS` is the set of separation artifacts plus the raw mix, and its
comment states the contract: "a target is always a single named source, never a
merged set." A strum is a reading of the guitar stem, not a source. Adding it as
a target would touch `VALID_TARGETS`, `TARGET_LABELS`, the ReaScript's
`TRANSCRIPTION_TARGETS`, and sidecar migration — and it would be wrong.

It is a **profile on the `guitar` target**, producing one variant. That also
gets the peer semantics right: a strum reading and a note reading are two
readings of one stem, which is what the variant model already means.

### 2. The profile registry special-cases drums *by target*, and guitar can't use that door

`_profile_for_target` resolves a stored profile name through
`_INSTRUMENT_PROFILES`, and `InstrumentProfile`'s field set describes a note
detector (`onset_threshold`, `frame_threshold`, `melodia_trick`, …). Drums
avoids it entirely: `backend_for_target_profile` and
`effective_profile_name_for_target` both branch on `target == "drums"` first and
resolve through the tiny `DrumTranscriptionProfile` registry instead.

Guitar has no such branch. So adding `"guitar-strum"` to
`_PROFILE_NAMES_BY_TARGET["guitar"]` without adding it to `_INSTRUMENT_PROFILES`
makes `_profile_for_target` pass the membership check and then **`KeyError` on
the registry lookup**. The two available fixes are a second by-target special
case (`if target == "guitar" and profile is a strum profile`), which entrenches
the smell, or a small refactor so profile→backend resolution is a lookup keyed
by *profile name* across the registries. Prefer the refactor: it is
behaviour-preserving, the existing profile tests cover it, and it is the
difference between one clean insertion point and a third special case.

### 3. Put strum settings in their own spec class

Adding the cleanup fields to `BasicPitchSpec` once bumped the stored
`settings_hash` of **every** basic-pitch target — a documented, expected,
zero-cost one-time re-transcription, but a needless one. A `StrumSpec`
alongside `DrumScriptSpec`/`AdtofSpec` keeps every other target's hash frozen,
which is the difference between "opt-in feature" and "everyone re-transcribes."

Two follow-ons:

- Every tuning constant must be *in* `StrumSpec.to_dict()`, not read from a
  module global by the detector. This is exactly what `CleanupStage`'s
  docstring exists to prevent: a constant outside the hash lets a retune leave
  a cached transcription silently stale.
- `isinstance(spec, (DrumScriptSpec, AdtofSpec))` appears at ~20 sites across
  `transcribe.py` and `transcription_variants.py`. A third event-shaped spec
  should land as a named union (`EventSpec = DrumScriptSpec | AdtofSpec |
  StrumSpec`) so a site that was missed is a type error, not a field that
  silently serializes `None`.

### 4. Quantization is a different operation from `drum_grid.reconcile_event_times`

`reconcile_event_times` exists because DrumScript emits times already quantized
to *its own* zero-anchored grid, and its guards deliberately leave a backend
alone when the onsets are **not** on a uniform grid — "a future unquantized
backend, whose real-second onsets need no correction and whose feel snapping
would destroy." A strum detector emits true onsets. Feeding them to that
function correctly does nothing, and making it act on them would defeat the
guard that protects the drum path.

Strum quantization is its own decision, with its own risk: snapping to the
nearest 8th/16th is what makes a *readable* pattern for a learner, and it is
also what throws away the swing and push/pull that the learner is trying to
copy. Ship the unquantized detector first and let quantization be a separate
profile, so the two are retained side by side as peers and can be compared —
which is what the variant model is for. And whatever it does, it only ever
*reads* the tempo grid: the "respect the project" invariant means this must
never write a tempo marker, and must degrade to unquantized output when no
tempo or time signature is known (the precedent is `_instantiate_cleanup`
dropping `clamp_sustain` entirely when tempo is unknown).

## Direction (down/up) is the weakest claim in the document, and should ship last

Read the document's own numbers in order:

- audio-only CNN on MFCCs: **~72 %** up/down accuracy;
- audio **+ a hand-mounted IMU**: ~92 %/85 % — and the direction label there
  comes from the accelerometer, not the audio;
- the CRNN's >87 % per-direction class: synthetic-trained, evaluated on pickup
  signals.

The honest reading is that audio-only direction is unsolved at a quality that
helps a learner, and that the impressive figures come from a sensor strapped to
the player's wrist. A strum track that gets the arrow wrong a quarter of the
time is worse than one with no arrows, because a practising user will trust it.

There is also a free baseline that must be beaten before any classifier earns
its place, and it is the one a teacher would actually write: **alternate
down/up by grid position.** If a detector's direction accuracy does not clearly
exceed "downstroke on the beat, upstroke on the off-beat", the classifier is
not adding information. This is finding 6 — the crude measure keeps winning —
applied before the work rather than after it.

So: emit onsets (plus velocity, which carries accent for free and costs
nothing) first. Add direction only against a hand annotation that contains
directions, and reserve a third pitch for "unknown" rather than guessing.

## Two suggestions in the document to decline

**REAPER markers as the output.** Markers and regions are the section
machinery, with durable ownership tracking (`managed_region_ids`, project
ext-state written before mutation, deletion by ID identity) that exists because
getting it wrong destroys user data. A MIDI variant track costs nothing and
reuses a path that already works.

**"Feedback generation" / a pattern-match score of expected vs. played.** This
is the retired practice-workflow milestone (issues #89 and #105), which
`docs/AGENTS.md` says is not planned work and must not be resurrected into new
issues without an explicit human decision. Detecting the pattern is in scope;
grading the user against it is not.

Also not applicable: the document's Gantt chart and its "create a custom
dataset, train on a V100" plan. Work here is issues with `blocked_by`
dependencies, and there is no CI and no GPU story.

## Phase 0: the reference annotation (human-owned, blocks everything else)

**Status: not started.** Nothing else in this document can begin until this
exists, because without it every claim about detector quality is a guess. This
is human-owned REAPER work per `docs/AGENTS.md`, so it must not be filed as an
agent-runnable issue.

### What the annotation is

A **rhythm track for the strumming hand** — the right-hand equivalent of a drum
reference. One mark per *stroke*, not per string: strumming a six-string G chord
is one mark, not six. The pitch of each mark carries no meaning; it is a flag on
a timeline, not a transcribed note.

This is deliberately a different question from the one
`[vgt] Guitar Ref — … (MIDI)` already answers. That track holds the voicings
(which notes sound). This one holds the hand rhythm (when a stroke happened).
Six notes on beat 1 tells you the chord; one mark on beat 1 tells you there was
a stroke there. Merging them into one track would make both unreadable.

### How to prepare it

In the 7Rivers project, add a MIDI track named plainly — `strum reference` —
and specifically **not** `[vgt]`-prefixed, so vgt never treats it as its own.
It is found by track name (the same handoff the bass reference used).

Four requirements that carry the measurement:

1. **Each mark starts at the real attack in the audio**, aligned against the
   guitar stem's waveform — *not* quantized to the grid. Quantizing destroys the
   only evidence of whether a detector's timing is right, which is most of the
   point.
2. **Mark the leading edge of the stroke.** A strum rakes across the strings
   over roughly 20–40 ms, low to high; the first string contacted is what any
   onset detector fires on, so it is the fair comparison. Be consistent — the
   convention matters less than applying it the same way throughout.
3. **Every stroke inside the window, including quiet, muted, and ghost
   strokes.** An unmarked real stroke makes a correct detection score as a false
   alarm, and over-detection is the failure mode this project has been burned by
   twice (see findings 3 and 5).
4. **One continuous 20–30 s stretch**, and record its exact start/end times so
   scoring is confined to the annotated span. Continuous beats scattered
   snippets. A stretch containing a pattern change or some muted strumming is
   worth more than a uniform one — that is where detectors fail.

Deliberately **not** required, so no time goes into them: note lengths (only the
start is read), velocity (optional, only if marking accents is interesting), and
direction. Direction is optional and can be added later; if it is annotated, use
two pitches (C3 = down, D3 = up) and say so.

A 20–30 s window is genuinely enough to start. Bass's tuning was fitted to
15.7 s and picked the same settings the eventual 272-note full-length reference
picked. Extending the annotation later is expected, and per finding 2 every
conclusion drawn from the short version gets re-checked against the longer one.

### A shortcut, with one warning

Solo the guitar stem, tap along on a MIDI track in record, then drag each note
onto its waveform attack. Faster for getting the right *number* of marks
roughly in place.

The dragging step is not optional. Uncorrected tapped notes carry human
reaction time, which is about the size of the ±50 ms tolerance being measured —
the result would mostly measure the tapping, not the detector.

### Handoff

Save the project and say where it is. The stem audio is needed alongside the
annotation, and per the round-three limitation in
[guitar-transcription-findings.md](guitar-transcription-findings.md) the
7Rivers stem lives in the maintainer's own project rather than this repo — so
its location is an **open question** to settle at handoff time. Only the
extracted timings get committed, as a numbers-only fixture beside
`tests/fixtures/bass_7rivers/`; no stem audio enters the repo.

## Recommended sequence

Each phase is gated on the previous one's measurement, and each is small enough
to be one issue. Phase 0 above blocks phase 1; nothing here starts before it.

**Phase 1 — measure classical detectors, ship nothing.**
`scripts/strum_detection_probe.py`, evaluation-only, in the shape of the
existing probes. Score spectral flux, superflux, complex-domain, and
HPSS-percussive+flux against the annotation using `drum_evaluation`'s existing
±50 ms one-to-one matcher, reading the reference through `drum_midi_score.py`'s
SMF reader. Report **precision alongside recall** — over-detection inside a
sustained strum is precisely the failure this repo has been burned by twice —
plus the grid-alternation direction baseline. Add a row to the probes table in
`instrument-transcription-findings.md` and write
`docs/strum-transcription-findings.md`.

**Phase 2 — ship the detector as an opt-in variant,** if phase 1 clears a bar
agreed in advance (an onset F comparable to the ~75–80 % the literature gives
classical detectors on clean guitar would be a reasonable bar on a separated
stem; pick it *before* seeing the numbers). New `src/vgt/strum.py`, `StrumSpec`,
a `guitar-strum` profile, the registry refactor from §2, the `EventSpec` union
from §3. Opt-in via `vgt transcription variant add guitar --name strum
--profile guitar-strum`. Document in the manual that it belongs as a *variant*,
not as `--mode guitar=guitar-strum` — the latter would replace the user's note
reference, which is non-destructive but surprising. Every note target's
`settings_hash` must be unchanged; assert it in the existing hash tests.

**Phase 3 — quantized variant and, separately, direction,** each measured
against the same annotation, each retained as a peer rather than replacing the
unquantized one.

**Phase 4 — ML, only if someone else trained it.** If public weights appear
(re-check the papers above), a pinned subprocess backend is the established
pattern. But note what ADTOF cost for exactly this shape of dependency: a git
pin, a bundled converted checkpoint with a recorded SHA-256, a lock file, an
isolated interpreter, and a timeout. Training our own model in this repo is out
of scope — it would be the largest thing here, unverifiable offline, and would
make us the vendor of our own unversioned weights.

## Invariant check

| Invariant | Effect |
| --- | --- |
| Non-destructive / working-copy boundary | Safe: one new `[vgt]`-owned variant track, via the existing apply path |
| Idempotent | Safe **iff** every strum tuning constant is inside `StrumSpec`'s serialized identity (§3) |
| Live REAPER mutation | Unchanged — no ReaScript edits at all |
| Analysis outside REAPER | Satisfied: detection is Python/librosa |
| Correctable | A strum variant is a disposable audio-derived draft, like chords; the manual must say so per backend |
| Separate ownership and evidence | Satisfied: a peer variant, never the guitar default, never "best" |
| Respect the project | **Watch item:** quantization reads the beat grid and must never write a tempo marker (§4) |
| Cost safe | Safe: local, free, no credentials — provided phase 4's constraints hold |
