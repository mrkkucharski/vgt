# Instrument transcription findings — index

The entry point for **per-instrument transcription quality**: which backend each
target uses, how good its output actually is, what was measured to establish
that, and what is still open. Each instrument's evidence lives in its own
findings document; this page is the map plus the shared method.

This is deliberately separate from the planning docs. `transcription-plan.md` and
`transcription-variants-plan.md` describe the *machinery* (specs, hashes,
variants, caching). The documents indexed here describe *how well it transcribes
a given instrument, and why* — the accumulated per-instrument knowledge.

## Status per target

| Target | Backend | Profile(s) | Measured? | Findings |
| --- | --- | --- | --- | --- |
| `guitar` (acoustic) | Basic Pitch | `guitar-acoustic-clean` / `-detail` / `-strict-chords` | Yes — one acoustic stem, three rounds | [guitar-transcription-findings.md](guitar-transcription-findings.md) |
| `guitar` (electric) | Basic Pitch | `guitar` | **No** — deliberately left at shared defaults | see guitar findings, "Changes applied" §1 |
| `guitar` (strum onsets) | Classical onset candidates (evaluation only) | — | **Rejected** — best 7Rivers F1 24.0%, precision 17.1%; no variant shipped | [strum-transcription-findings.md](strum-transcription-findings.md) |
| `guitar` (MT3 alternative) | MT3, opt-in | `guitar-mt3` | Yes — one 7Rivers run: no sustain runaway, chord-tone agreement 94.4% (vs. clean default's 68.4%), but heavy fragmentation (1228 of 1804 notes); **ear-verified on a second song too — MT3 seems better at catching chords** | [guitar-transcription-findings.md](guitar-transcription-findings.md), "MT3 alternative" |
| `bass` | **pYIN** (monophonic tracker) + re-articulation split | `bass` / `bass-pyin`; `bass-basic-pitch`, `bass-monophonic` retained for comparison | Yes — one 7Rivers stem: frame F 78.9%, octave errors 10.9%; **onset F 75.6% against a 272-note full-length hand annotation** (57.1% before splitting); earlier Basic Pitch experiments retained as evidence | [bass-transcription-findings.md](bass-transcription-findings.md) |
| `bass` (MT3 alternative) | MT3, opt-in | `bass-mt3` | Yes — one 7Rivers run: raw output is a known octave convention apart from the default (not leakage — a −12 semitone shift moves frame F from 0.0% to 27.0%), but even octave-corrected it is still clearly behind pYIN's 78.9% F; **ear-verified on a second song too — current pYIN default sounds better on both; not recommended** | [bass-transcription-findings.md](bass-transcription-findings.md), "MT3 alternative" |
| `drums` | DrumScript (`raw` default, optional `hpss`), ADTOF (`adtof`) | `raw`, `hpss`, `adtof` | Yes — IDMT-SMT-Drums corpus + real-stem timing study | [drumscript-evaluation-findings.md](drumscript-evaluation-findings.md), [drums-transcription-timing-findings.md](drums-transcription-timing-findings.md), [drums-clean-profile.md](drums-clean-profile.md), [adtof-phase-0-feasibility-findings.md](adtof-phase-0-feasibility-findings.md) |
| `vocals` | Basic Pitch | `vocals` | **No** — frequency window only | — |
| `piano`, `strings`, `instrumental`, `backing`, `original` | Basic Pitch | `default` | **No** — full-range defaults | — |

"Measured?" means someone quantified output quality against a reference and wrote
down the numbers. An unmeasured target is not known to be bad — it is *unknown*,
which is exactly what this index exists to make visible.

## What has been learned so far

Six findings have held across more than one instrument, and are the things to
check first on a new one.

**1. `frame_threshold` and `melodia_trick` must move together.** Raising the
frame threshold while leaving melodia on is *worse than the default* on both
guitar (chord agreement 52.5% → 37.5%) and bass (F stuck at 7.6% with polyphony
still at 22). Releases start happening, and melodia reconnects them into more,
shorter, wronger notes. Never sweep one without the other.

**2. Sustained material breaks Basic Pitch's note release.** A continuously
strummed acoustic and a bass stem fail the same way: activations never fall below
`frame_threshold = 0.3`, so notes are never released and melodia glues them into
multi-minute drones. Both produced a ~120 s note and ~22-voice polyphony from
completely different instruments. Expect this on anything sustained — organ, pads,
bowed strings.

**3. Cleanup cannot substitute for detection, and detection cannot substitute for
cleanup.** On guitar, applying cleanup to the *baseline* output left 2077 notes
that were still wrong — the settings had to be fixed first. But no guitar setting
got polyphony to six either, so the cleanup pipeline is not optional. Bass is the
extreme case: neither lever was enough and the backend had to change.

**4. Pick the backend from the instrument's voice count, not from convenience.**
Basic Pitch is polyphonic and piano-trained. For a genuinely single-line source it
is the wrong tool, and no "keep one note" post-filter repairs it, because the
model's note *boundaries* are wrong — a filter can only choose among wrong
candidates. Bass moved to a monophonic F0 tracker and went from F 7.2% to 78.9%.
Conversely, do **not** apply a monophonic tool to `vocals`: LALAL vocals stems
routinely carry stacked backing vocals and harmonies that are genuinely
polyphonic.

**5. A metric can be blind to a whole failure mode, and the strongest evidence
is the metric you do not have.** Bass's frame-level F-measure — the number step 3
below tells you to trust — cannot distinguish one held note from four repeated
plucks of the same fret, because the right pitch is sounding either way. Across a
change that recovered a third of the notes in the part, every frame-level column
was unchanged *to the decimal*. Both reference estimators shared the blind spot
exactly, because both are frame-level. What exposed it was a human listening in
REAPER and hand-correcting the MIDI. Before concluding a target is fine, ask what
its metric would look like if a specific musical failure were present — and if
the answer is "identical", that failure is unmeasured, not absent. Repeated notes
are the case to check on any monophonic target; drums avoid it only because their
metric is onset-based to begin with.

**6. On a real stem the crude measure keeps winning.** Bass re-articulation was
attacked with five detectors: a decaying peak follower (models a plucked
string's physics), a locally adaptive threshold (models the passage getting
quieter), sharper and band-limited envelopes (models the attack being brief),
beat-grid-guided candidates (models the part being rhythmic), and mel-band
spectral flux. All five lost to differencing a heavily smoothed energy envelope
and then constraining *where* the results may land. Two of them lost to the
feature they were meant to improve: pYIN's own 93 ms RMS window beat every
sharper envelope, because the smearing is useful smoothing. Prefer the simple
feature plus a structural constraint, and make the better-motivated model earn
its place against a measurement.

A corollary that cost real time: **the cleanup pipeline's order is load-bearing on
every instrument.** `clamp_sustain` must precede anything that resolves overlaps
or counts voices, because a runaway drone otherwise dominates the decision.
Moving it across `force_monophony` swung bass accuracy ~20 points; the same
mistake in the guitar pipeline re-created a 7.1 s note under a 4 s clamp.

**7. "First output track" has a precise, verified mechanism — it is not
arbitrary, but it is also not a content guarantee, and a pitch mismatch is not
automatically evidence of picking the wrong instrument.** Traced in MT3's own
source rather than assumed: `mt3/note_sequences.py::assign_instruments` gives
instrument index 0 to whichever GM program's note it decodes *first*, index 1
to the next new program encountered, and so on — except any drum note, which
always gets a fixed instrument index (9) regardless of when it occurs;
`note_seq`'s MIDI writer then emits tracks sorted by that instrument index, and
`mt3/metrics_utils.py` decodes audio segments in strict chronological order.
So **"first track" = whichever non-drum instrument genuinely sounds first in
the piece**, drums structurally excluded from ever winning that slot.

Confirmed by re-running `mt3-transcribe` directly (outside vgt, keeping its
full multi-track output) on both the 7Rivers guitar and bass stems used
above. Both times, track selection was **correct**: bass's first track was
labeled "Acoustic Bass" (program 32, 131 notes, first onset at tick 35, vs.
the next spurious track — piano — at tick 4765); guitar's first track was
labeled "Acoustic Guitar (nylon)" (program 24, 1804 notes, first onset at
tick 26). Drums were present in the guitar stem's raw output (244 notes,
first onset at tick 260 — earlier than 12 of the other 13 detected tracks)
and were still correctly excluded from the first slot by the hard-coded rule,
not luck.

That means neither measured result above is actually a *track-selection*
failure: bass's low accuracy (even octave-corrected, 27.0% frame hit vs.
pYIN's 82.6%) is a genuine note-level transcription problem on the content
MT3 correctly identified as bass. Guitar's severe fragmentation has a second,
now-confirmed contributor beyond simple same-pitch splitting: MT3 detected
the *same* real guitar performance as two separate near-identical programs
("Acoustic Guitar (nylon)", kept, 1804 notes; "Electric Guitar (jazz)",
discarded, 105 notes) — content vgt's "first track only" rule silently drops,
not because it picked the wrong instrument, but because MT3 itself does not
reliably keep one instrument in one program. Treat a first-track pitch or
content mismatch as an open, checkable question (verified mechanism above,
octave/notation convention, or genuinely noisy/split output) rather than an
assumed leakage failure — but do not assume it is *never* a wrong instrument
either: the hard exclusion is drums-specific; nothing stops two genuinely
different pitched instruments from racing for "first" on a stem with real
bleed, this is simply not what happened on either measured song here.

## Method

The recipe that produced both the guitar and bass findings, in order:

1. **Quantify the complaint before changing anything.** Note count, note-length
   distribution, peak *and median* polyphony, and pitch range against what the
   instrument can physically play. Record it as the baseline row.
2. **Establish a reference you did not transcribe with.** Guitar used vgt's own
   detected chords (a relative signal only); bass originally used two estimators
   from different algorithm families — pYIN (time-domain) and a CQT harmonic sum
   (frequency-domain) — and reported their 85.9% agreement, so neither one's
   failure modes explain the other's.

   **Ask the maintainer for a hand-corrected track before assuming an estimated
   reference is the best available.** Bass now has one: 272 notes over the full
   track, hand-corrected in REAPER, committed as numbers-only fixtures. It cost
   an afternoon of the maintainer's time and it settled questions no estimator
   could (see finding 5). Two practical notes learned doing it:

   - **A prefix is enough to start.** The first tuning was fitted to 15.7 s and
     picked the same settings the eventual full-length reference picks. Begin
     measuring against whatever exists rather than waiting for completeness.
   - **Re-check every claim when the annotation grows, and expect to retract
     some.** An interim 30.6 s snapshot showed held-out precision collapsing,
     which was written up as a real limitation; at 117 notes it was visibly
     small-sample noise. Report the sample size next to the number, and treat a
     conclusion drawn from a few dozen events as provisional.
3. **Pick a metric that penalizes over-detection.** This is the most important
   step and the easiest to get wrong. "Is a correct note sounding?" scores a
   22-voice mess at 90.8% because something is always right. Report precision
   alongside recall and read the F-measure. Guitar's equivalent trap: prefer the
   time-attributed chord metric (`%ct-t`) over the onset-attributed one (`%ct-on`)
   whenever variants differ in note length.
4. **Sweep the detector, then sweep the cleanup, then compare backends.** In that
   order, and record every variant's settings alongside its scores so the table is
   reproducible. Both findings docs carry the full settings table.
5. **Re-verify through the real CLI, not the sweep harness.** Guitar was
   re-transcribed via `vgt analyze --forget-transcription guitar --transcribe
   guitar`; the numbers a sweep script produces are not proof the shipped path
   does the same thing.
6. **Write down what you ruled out.** Guitar's "converting the stem to mono" and
   "stem quality" sections, and bass's `force_monophony` and lowest-pitch-wins
   attempts, exist so nobody re-runs them.

### Probes

Evaluation-only. Neither runs a backend nor writes into a vgt project.

| Script | For | Headline metrics |
| --- | --- | --- |
| `scripts/guitar_transcription_probe.py` | polyphonic targets | polyphony, fragmentation, harmonic-ghost share, chord agreement (`%ct-on` / `%ct-t`) |
| `scripts/bass_transcription_probe.py` | monophonic targets | polyphony, frame precision / recall / F against a pYIN or CQT reference, octave-error share; `--onset-reference` adds onset P/R/F against a hand annotation |
| `scripts/drumscript_benchmark.py` | drums | onset F-measure against IDMT-SMT-Drums annotations |
| `scripts/drum_midi_score.py` | drums | scores an event JSON or MIDI against a reference |
| `scripts/strum_detection_probe.py` | guitar strum onsets | classical onset precision / recall / F1 against a hand-stroke reference; candidate variants are evaluation-only |

`--onset-reference` is separate from the frame metrics for the reason finding 5
gives: it is the only column that moves when note *boundaries* change at an
unchanged pitch, and it needs a real annotation because no estimator derived from
the stem can supply one. `tests/fixtures/bass_7rivers/` holds bass's — 272 notes, the full track.

The two note-based probes report deliberately different things, because the
question differs: for a polyphonic instrument the useful question is how much of a
legitimate texture is spurious; for a monophonic one every simultaneous note is
spurious and the question is which single pitch was chosen. Do not port chord
agreement to a single-line instrument, or ghost share to one that cannot have
ghosts.

## Adding an instrument

When measuring a target for the first time:

1. Follow the method above and write `docs/<instrument>-transcription-findings.md`
   in the same shape: status header (including `settings_hash` impact), complaint
   quantified, root cause, sweep with a settings table, what shipped, real-world
   verification, what was ruled out, known limitations, reproducing.
2. Add a row to the status table on this page and, if the lesson generalizes, to
   "What has been learned so far".
3. If the instrument's failure mode needs a metric neither probe reports, add a
   probe rather than putting the numbers only in the doc — an unreproducible
   measurement stops being useful the moment the code moves.
4. Note the `settings_hash` consequence explicitly. Retuning a profile
   invalidates that target's cached transcription and re-runs it once, which is
   expected and zero-cost; changing a *shared* default touches every target and
   needs saying out loud.

## Standing caveat

Every instrument here is measured on **one track**, `7Rivers`, except drums (which
additionally has an annotated corpus). These findings rank variants reliably; they
are not absolute accuracy figures, and none of the outputs is ground truth. Every
target's reference MIDI is a **draft** for practice, and the user manual says so
per backend.
