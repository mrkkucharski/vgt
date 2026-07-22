# DrumScript evaluation findings

Status: evaluation-only evidence for the deferred D-F routing decision.  It
does not change `vgt analyze`, REAPER tracks, production artifact names, or
sidecar fields.

## Reproducible harness

The corpus selected for the full evaluation is **IDMT-SMT-Drums V2** (Zenodo
record 7544164, file `IDMT-SMT-DRUMS-V2.zip`, MD5
`d2664b4c2aaa34b90ba2f57b389c5663`).  It supplies manual kick, snare, and
hi-hat onset annotations.  Fraunhofer makes it available for evaluation under
CC BY-NC-ND 4.0; therefore neither its audio nor derived clips are committed
to vgt.  This makes the smoke check a deliberately user-supplied-fixture
command, outside the normal test suite.

The benchmark command deliberately consumes a small manifest plus normalized
DrumScript JSON, rather than downloading data or invoking a model:

```sh
# Generate a manifest from the selected IDMT annotations, then retain it with
# the run report outside this repository (audio remains external).
uv run python scripts/idmt_drum_manifest.py /path/to/IDMT-SMT-DRUMS-V2 idmt-manifest.json
uv run python scripts/drumscript_benchmark.py idmt-manifest.json \
  --events-dir idmt-drumscript-events --output idmt-report.json

# Explicit, potentially downloading integration check; output must be a
# disposable directory, not vgt/<namespace>/transcription/.
uv run python scripts/drumscript_smoke.py /path/to/redistributable-drums.wav /tmp/vgt-drumscript-smoke
```

`drumscript_benchmark.py` reports precision, recall, F1, TP, FP, and FN for
every observed/annotated class at the fixed 50 ms tolerance.  Its macro and
global figures provide context only; the individual cymbal rows remain visible.
Each clip is matched independently before TP/FP/FN are summed, so identical
relative timestamps from separate files can never be counted as a match.  The
JSON report carries the input manifest SHA-256 and corpus/source metadata for
the precise evaluated subset.
The deterministic checked-in fixture verifies the command shape and scoring
semantics without claiming to be a model-quality result:

```sh
uv run python scripts/drumscript_benchmark.py tests/fixtures/drum_evaluation/annotated-manifest.json \
  --events-dir tests/fixtures/drum_evaluation/events
```

| Class | Precision | Recall | F1 | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| crash | 0.000 | 0.000 | 0.000 | 0 | 0 | 1 |
| kick | 1.000 | 0.500 | 0.667 | 1 | 0 | 1 |
| ride | 0.000 | 0.000 | 0.000 | 0 | 1 | 0 |
| snare | 1.000 | 1.000 | 1.000 | 1 | 0 | 0 |
| macro | 0.500 | 0.375 | 0.417 | — | — | — |
| global | 0.667 | 0.500 | 0.571 | 2 | 1 | 2 |

This is an intentionally adverse, tiny regression fixture, not an IDMT claim.
It demonstrates that a weak cymbal class cannot disappear behind the global
number.  The full-IDMT results must be appended below before D-F considers a
route switch.

## IDMT-SMT-Drums V2 automated run

Run on 2026-07-21 against the first sorted official mix clip,
`RealDrum01_00#MIX.wav`, using the verified archive checksum above.  This is a
reproducible **one-clip smoke benchmark**, not a representative corpus result:
the deliberately small selection establishes the end-to-end evidence path
without silently converting an expensive corpus/model run into normal CI.

```sh
uv run python scripts/idmt_drum_manifest.py /tmp/IDMT-SMT-DRUMS-V2 idmt-manifest.json
uv run python scripts/drumscript_smoke.py /tmp/IDMT-SMT-DRUMS-V2/audio/RealDrum01_00#MIX.wav /tmp/vgt-drumscript-smoke
# retain just RealDrum01_00#MIX and rename the normalized event JSON to
# <clip-id>.json, then run drumscript_benchmark.py as above
```

| Dataset identity | DrumScript/runtime | vgt Python / uv | Clips | Tolerance |
| --- | --- | --- | ---: | ---: |
| IDMT-SMT-Drums V2 / Zenodo 7544164 / `d2664b4c2aaa34b90ba2f57b389c5663` | `drumscript==0.1.6`, isolated Python 3.12.13 | Python 3.11.15 / uv 0.11.7 | 1 | 50 ms |

| Class | Precision | Recall | F1 | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| kick | 0.000 | 0.000 | 0.000 | 0 | 2 | 23 |
| snare | 0.130 | 0.143 | 0.136 | 3 | 20 | 18 |
| hi_hat_closed | 0.312 | 0.357 | 0.333 | 15 | 33 | 27 |
| macro | 0.148 | 0.167 | 0.157 | — | — | — |
| global | 0.247 | 0.209 | 0.226 | 18 | 55 | 68 |

The output contained 49 DrumScript events and passed the normalized-MIDI/JSON
smoke validation.  This result is strongly negative evidence for a route
switch, especially the zero kick F1.  It is also too narrow to estimate
general performance: a future D-F decision needs a broader explicitly chosen
IDMT subset/full-corpus report and a cymbal-capable corpus, because IDMT's
annotations only cover kick, snare, and hi-hat.  No user-song listening or
REAPER work was involved.

## Temporary Basic Pitch shadow diagnostic

```sh
uv run python scripts/drumscript_shadow_compare.py drumscript-events.json basic-pitch-notes.csv \
  --output /tmp/vgt-drum-shadow.json
```

The report is named `temporary-evaluation-only-shadow-comparison`.  It groups
Basic Pitch note starts within 20 ms into unlabeled transient clusters, then
one-to-one matches them with DrumScript event onsets within 50 ms.  It only
contains `matched`, `drumscript_only`, and `basic_pitch_only` counts.  It does
not map Basic Pitch pitches to drum instruments, filter events, create MIDI or
REAPER tracks, write a production artifact name, or represent confidence.
The command rejects output paths in a vgt `transcription` or `.vgt` sidecar
directory (and `drums.json`/`drums.mid` names), making this boundary enforceable
as well as documented.

## Limits and human-owned work

The integration smoke input must be redistributable or user-owned.  Real-song
listening, usefulness judgments, and any REAPER verification remain optional,
human-owned work and are neither test requirements nor blockers for this
evaluation tooling.

## D-F rollout decision (2026-07-22)

[docs/drumscript-plan.md](drumscript-plan.md) makes the production route change
conditional: "If acceptable based on the automated and audio-quality evidence,
change the single target-routing table so `drums` selects DrumScript." The D-E
evidence gathered above does not clear that bar on its own:

- The only corpus-scale result is a **one-clip smoke benchmark** against
  `RealDrum01_00#MIX.wav`, explicitly documented above as "not a representative
  corpus result."
- On that clip, **kick F1 is 0.000** (0 TP, 2 FP, 23 FN) — DrumScript missed
  essentially every kick onset. Snare F1 is 0.136 and hi-hat-closed F1 is
  0.333. Global F1 is 0.226.
- The findings already flag this as "strongly negative evidence for a route
  switch" and state that "a future D-F decision needs a broader explicitly
  chosen IDMT subset/full-corpus report and a cymbal-capable corpus" — evidence
  that has not been produced.
- No real-LALAL-stem or bleed/artifact evaluation (plan section "Quality
  evaluation", items 2-3) has been run or recorded here.

An initial D-F pass therefore stopped short of switching the route and marked
the issue `status:blocked` for a human rollout decision, per the plan's own
instruction not to guess or silently broaden scope. The repository owner then
reviewed this evidence directly on the issue and explicitly instructed the
work to proceed ("Finish the work - I did the verification.",
[issue #98](https://github.com/mrkkucharski/vgt/issues/98#issuecomment-5041703100)).
That is the third option this document originally posed to a human reviewer:
accept the current accuracy for a specific product reason and explicitly
override the plan's quality bar — "any drum groove reference beats none" is a
defensible product call even against a weak single-clip benchmark, since vgt
never claims calibrated confidence for DrumScript output and the user manual
documents the limitation plainly.

Given that explicit human override, **D-F switches the production routing
table.** `production_transcriber_router()` in `src/vgt/transcribe.py` now
passes `drumscript_targets=("drums",)`, so `drums` routes to
`DrumScriptTranscriber` by default; every other target continues to route to
`BasicPitchTranscriber`. `docs/GOAL.md` and `docs/USER-MANUAL.md` were updated
to describe this as the shipped behavior, including DrumScript's channel-10 GM
percussion semantics, fixed velocity, and lack of calibrated confidence.

This was already an implementation-complete capability going into the
decision: `DrumScriptTranscriber`, its validation, cache/status/forget
integration, and the offline REAPER import contract were implemented and
tested (243 tests passing before and after the route switch). No shadow
comparison or evaluation code was ever wired into `vgt analyze` (see
`src/vgt/drum_evaluation.py`'s module docstring), so there was no shadow
output to disable in production.

The weak automated benchmark evidence above remains valid and should still
guide expectations: users should treat `[vgt] Drums Ref (MIDI)` as a rough
draft, most reliable for gross onset placement and least reliable for kick
detection, until a broader corpus/real-stem evaluation is run.
