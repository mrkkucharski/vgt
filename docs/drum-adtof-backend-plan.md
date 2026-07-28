# Plan: alternative drum ADT backend (ADTOF-pytorch + vgt-owned post-processing)

Status: plan / design. Grounds the work in the existing transcription
architecture and the engine recommendation in
`docs/drum_adt_recommendation.pdf`. No code changes are made by this document.

## Goal

Add a **second, opt-in** automatic drum transcription (ADT) backend for the
`drums` target, without removing the current one:

- **Keep DrumScript as the baseline.** It stays the default drum backend; every
  existing `default` / `drums-clean` variant, its identity hash, its cache, and
  its output contract are unchanged.
- **Add an `adtof` backend** that uses **ADTOF-pytorch as a classification
  engine only**: vgt runs the model to obtain its **raw per-frame activations**
  and then does **peak picking, beat-grid association, velocity estimation, and
  MIDI/JSON authoring in vgt's own pipeline** (per the recommendation doc's
  primary option). This maximises vgt's control over timing — the exact thing
  DrumScript gets wrong (see `docs/drums-transcription-timing-findings.md`).
- **Both coexist as retained variants of the same target.** The variant system
  already supports several candidates per target (immutable ids, editable
  labels, none preferred); an ADTOF variant is just another candidate a user
  can add alongside the DrumScript ones and compare.

Non-goals: replacing DrumScript, changing the default backend, changing any
other target (guitar/bass/etc. stay on Basic Pitch), or shipping a paid engine.
The commercial fallbacks in the recommendation (Superior Drummer 3 Tracker,
Klangio) are explicitly out of scope here.

## Why this fits vgt cleanly

The transcription layer already has every seam this needs:

- **Backend seam** — `Transcriber` protocol
  (`transcribe(source, dest, spec, progress) -> TranscriptionResult`) in
  `src/vgt/transcribe.py`. DrumScript and Basic Pitch are just two
  implementations; ADTOF becomes a third.
- **Router seam** — `TranscriberRouter` / `TargetTranscriberRouter.for_target`.
  Today it picks a backend **by target name** (`drums` → DrumScript). The one
  real structural change this plan needs is to make backend selection
  **profile-driven** so two backends can serve the same target.
- **Spec + identity** — each backend has a frozen spec (`BasicPitchSpec`,
  `DrumScriptSpec`) whose `to_dict()` feeds the variant `settings_hash`. ADTOF
  gets its own `AdtofSpec`; being new, it carries no legacy-hash constraints.
- **Variant lifecycle** — `vgt transcription variant add <target> --name <label>
  --profile <name>` creates and reconciles retained variants. If the ADTOF
  backend produces the same on-disk output contract DrumScript does
  (`transcription.mid` + `transcription.json`, GM percussion channel 10,
  validated), the lifecycle, caching, ReaScript apply, and sync all work
  unchanged.
- **Beat grid already plumbed** — `spec_for_target(..., beat_times,
  downbeat_offset_s)` already flows vgt's analysis beat grid into the drum
  spec (it feeds `drums-clean`). The ADTOF post-processor reuses it directly
  for grid association.
- **Project-tempo authoring already established** — issue #193 added
  `DrumScriptSpec.midi_tempo` and made the drum path author MIDI at the
  project's tempo via `_write_midi`. ADTOF authoring reuses the same
  `_write_midi(..., midi_tempo, channel=9)` path, so it is born correct on
  timing.
- **Isolation model** — heavy/native backends run only as **pinned
  subprocesses** in a throwaway temp dir, their output validated before it
  enters the sidecar namespace (DrumScript uses `uvx`). ADTOF (Torch) follows
  the same rule so its dependencies never enter vgt's own environment.

## Architecture

```
drum stem (LALAL) ──► [adtof runner: isolated pinned subprocess]
                          └─ ADTOF-pytorch model inference (Torch, eval)
                          └─ dumps RAW activations: [n_frames × n_classes] + meta (frame rate, class order)
                                     │  (.npz in temp dir, validated)
                                     ▼
                       [vgt post-processing — in-process, numpy only]
                          1. peak picking   (per class: threshold + local max + min inter-onset interval)
                          2. grid association (relate/snap onsets to vgt beat grid + downbeat)
                          3. velocity        (activation peak height and/or audio-envelope at onset)
                          4. class → GM note  (kick36 / snare38 / HH42·46 / tom45·48 / cymbal49·51)
                          5. author           transcription.mid (channel 9, project tempo) + transcription.json
                                     │
                                     ▼
                       identical output contract to DrumScript ──► variant lifecycle (unchanged)
```

Key decision: **the subprocess emits raw activations; vgt owns everything after
that.** This is the recommendation doc's "retrieve raw activations and do
peak-picking/grid/velocity/MIDI yourself" — and it keeps grid association (the
timing win) inside vgt where the beat grid lives, while confining Torch to the
subprocess.

## Backend selection (the one structural change)

Backend must be chosen per **profile/variant**, not per target, so DrumScript and
ADTOF can both target `drums`. Proposed approach:

- Introduce a drum **engine selector** carried on the resolved profile/spec
  (e.g. a built-in `drums-adtof` profile whose `backend == "adtof"`, versus the
  existing DrumScript-backed `default` / `drums-clean`).
- Have the transcribe dispatch (router or the drum-route resolver) pick the
  `Transcriber` from the resolved profile's backend rather than from the target
  name alone. Basic Pitch targets are untouched.
- User surface — no new command, reuse the existing variant flow:
  ```
  # baseline (unchanged, still the default):
  vgt --transcribe drums ...
  vgt transcription variant add drums --name clean --profile drums-clean
  # new alternative, coexists as another candidate:
  vgt transcription variant add drums --name adtof --profile drums-adtof
  ```

Because drums currently bypass `_INSTRUMENT_PROFILES` and select cleanup via
`modes`, Phase 1 decides the concrete shape (extend the drum profile/mode
mechanism vs. give drums first-class profiles with a `backend` field) — the
constraint is only that it stays cache-safe (existing DrumScript variant hashes
unchanged) and that the fake-backed path keeps the offline suite from importing
Torch.

## Output contract the `adtof` backend must honour

Same as DrumScript, so the lifecycle needs no special-casing:

- Writes `transcription.mid` (single track, **GM percussion channel 10 /
  encoded low-nibble 9**, notes only) authored at `spec.midi_tempo` (project
  tempo) via `_write_midi`, and `transcription.json` events.
- Returns a `TranscriptionResult` with `midi_path`, `events_path`,
  `instrument_counts`, `event_count`, `first_event_s`/`last_event_s`,
  `backend_tempo=None` (ADTOF gets its grid from vgt, not itself),
  `midi_tempo=spec.midi_tempo`.
- Validated with the existing `_validate_drumscript_midi` (or an equivalent) —
  reject non-percussion notes, empty output handled the same way.

## Risks / open questions (resolved by Phase 0)

- **License (compliance check, not a gate).** vgt is a **non-commercial hobby
  project**, so copyleft (AGPL/GPL) code and **research / non-commercial-only**
  model weights — common for ADT engines — are acceptable, not disqualifying.
  What remains is ordinary hygiene: confirm the exact package/weights license,
  keep upstream notices, and honor copyleft terms if the repo is ever published.
  This no longer blocks the effort; it is a routine step in Phase 0.
- **Exact package + model + weights.** "ADTOF-pytorch" must be pinned to a
  concrete PyPI/VCS package and model version, with a reproducible weights
  provenance (bundled or cached, hashed). Recorded in `AdtofSpec` so variant
  identity is stable and cache-keyed.
- **Activation API.** Confirm the port exposes raw per-frame activations (not
  just decoded onsets), the frame rate/hop, and the class ordering/label set
  (expected ~5 classes: kick, snare, hi-hat, toms, cymbals). Phase 0 dumps a
  real activation matrix on `7Rivers` drums.wav to verify.
- **Determinism.** CPU inference in eval mode, fixed seeds; record versions in
  the spec so results are reproducible and cache-safe.
- **Cost/perf.** Torch model inference on CPU is heavier than DrumScript;
  measure runtime on a typical stem. No network at inference time (weights
  pre-fetched/cached).
- **Test isolation.** The normal offline suite must never import Torch or hit
  the network; a `FakeAdtofTranscriber` (deterministic events) is injected
  through the router seam, exactly as `FakeTranscriber` is for the others.

## Delivery phases (tracked as separate issues, ordered by "blocked by")

0. **Feasibility spike & version decision** — pin the package + model + weights
   provenance, confirm the raw-activation API and class/frame contract, dump a
   real activation matrix on the `7Rivers` drum stem, and do a routine license
   check (compliance only — non-commercial hobby use, so copyleft/non-commercial
   licenses are fine). Output: the pins the later phases hard-code. *(Blocks the
   later phases only because they depend on its pins, not on a licensing
   go/no-go.)*
1. **Backend-selection seam + `AdtofSpec` + built-in `drums-adtof` profile
   (fake-backed).** Make backend selection profile-driven; register the ADTOF
   spec and profile; wire a `FakeAdtofTranscriber` so the variant flow works
   end-to-end with no real model yet. Existing DrumScript variant hashes
   unchanged. *(Blocked by 0.)*
2. **ADTOF activation runner** — isolated, pinned subprocess that runs the model
   and emits validated raw activations (+ frame-rate/class metadata) into a temp
   dir; error handling, timeout, and cache keying by (package/model version,
   stem hash). *(Blocked by 0.)*
3. **vgt post-processing → grid-aligned MIDI/JSON at project tempo** — peak
   picking, beat-grid association (using `beat_times`/`downbeat_offset_s`),
   velocity estimation, class→GM mapping, and authoring via `_write_midi`.
   Produces the DrumScript-identical output contract. *(Blocked by 1 and 2.)*
4. **Fake backend hardening, offline tests, goal-contract coverage, and docs** —
   unit tests for post-processing, an ADTOF variant lifecycle exercised offline
   via the fake in the goal contract, and user-manual/docs updates describing
   the opt-in alternative and how to compare it against the DrumScript baseline.
   *(Blocked by 1 and 3.)*

## Evaluation

Reuse the existing offline scorer (`scripts/drum_midi_score.py`, issue #182) and
the human-corrected `[work]` reference fixture
(`tests/fixtures/drums_7rivers/`, measures 3–30 only — restrict candidates to
that window). Success = the ADTOF variant beats the DrumScript baseline on
onset F1 and median timing error against that reference, with notes landing on
vgt's beat grid. The scorer already exists, so this is measurement, not new
scaffolding.
