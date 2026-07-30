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
| `bass` | **pYIN** (monophonic tracker) | `bass` / `bass-pyin`; `bass-basic-pitch`, `bass-monophonic` retained for comparison | Yes — one stem, 11-variant sweep | [bass-transcription-findings.md](bass-transcription-findings.md) |
| `drums` | DrumScript (default), ADTOF (opt-in) | `drums-clean`, `drums-adtof` | Yes — IDMT-SMT-Drums corpus + real-stem timing study | [drumscript-evaluation-findings.md](drumscript-evaluation-findings.md), [drums-transcription-timing-findings.md](drums-transcription-timing-findings.md), [drums-clean-profile.md](drums-clean-profile.md), [adtof-phase-0-feasibility-findings.md](adtof-phase-0-feasibility-findings.md) |
| `vocals` | Basic Pitch | `vocals` | **No** — frequency window only | — |
| `piano`, `strings`, `instrumental`, `backing`, `original` | Basic Pitch | `default` | **No** — full-range defaults | — |

"Measured?" means someone quantified output quality against a reference and wrote
down the numbers. An unmeasured target is not known to be bad — it is *unknown*,
which is exactly what this index exists to make visible.

## What has been learned so far

Four findings have held across more than one instrument, and are the things to
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

A corollary that cost real time: **the cleanup pipeline's order is load-bearing on
every instrument.** `clamp_sustain` must precede anything that resolves overlaps
or counts voices, because a runaway drone otherwise dominates the decision.
Moving it across `force_monophony` swung bass accuracy ~20 points; the same
mistake in the guitar pipeline re-created a 7.1 s note under a 4 s clamp.

## Method

The recipe that produced both the guitar and bass findings, in order:

1. **Quantify the complaint before changing anything.** Note count, note-length
   distribution, peak *and median* polyphony, and pitch range against what the
   instrument can physically play. Record it as the baseline row.
2. **Establish a reference you did not transcribe with.** Neither instrument had
   hand annotations. Guitar used vgt's own detected chords (a relative signal
   only); bass used two estimators from different algorithm families — pYIN
   (time-domain) and a CQT harmonic sum (frequency-domain) — and reported their
   85.9% agreement, so neither one's failure modes explain the other's.
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
| `scripts/bass_transcription_probe.py` | monophonic targets | polyphony, frame precision / recall / F against a pYIN or CQT reference, octave-error share |
| `scripts/drumscript_benchmark.py` | drums | onset F-measure against IDMT-SMT-Drums annotations |
| `scripts/drum_midi_score.py` | drums | scores an event JSON or MIDI against a reference |

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
