# Multiple transcription variants per stem

Status: design plan, 2026-07-24.

## Goal

Allow a user to retain several independently configured MIDI transcriptions of
the same source stem, compare them side by side in REAPER, select the most
useful result, create a user-owned working copy for manual editing, and discard
rejected generated candidates safely.

The motivating guitar workflow is:

- a **detail** candidate that preserves questionable notes and therefore
  contains more noise, but is less likely to hide a quiet or unusual note;
- a **clean** candidate that is easier to read as chord shapes because it
  applies the complete acoustic-guitar cleanup pipeline; and
- optionally, a **strict-chords** candidate whose higher Basic Pitch
  thresholds produce a deliberately sparse chord-oriented reference.

This feature is local and credit-free. It consumes an existing stem (or the
explicit `original` target), never triggers LALAL separation, and preserves
vgt's non-destructive and idempotent project contract.

## Current behavior and constraint

Transcription is already independent per instrument target. Each target has:

- a resolved backend/specification;
- an input hash and settings hash;
- cached MIDI plus CSV or JSON artifacts;
- a retained success, missing-source, or error record; and
- one adjacent `[vgt] <Target> Ref (MIDI)` track.

The target name is also the unique identity:

```text
analysis.transcription.targets["guitar"]
transcription/guitar.mid
transcription/guitar.csv
```

Consequently, changing the guitar profile invalidates and replaces the only
guitar transcription. The existing `--mode guitar=<profile>` map can select one
profile for guitar but cannot keep two results.

The implementation should generalize identity from `target` to
`target + immutable variant id`. It should not introduce a parallel
transcription subsystem.

## Product model

### Terms

- **Target:** source role such as `guitar`, `bass`, `vocals`, `drums`, or
  `original`.
- **Profile:** reusable declarative transcription settings, such as
  `guitar-acoustic-clean`.
- **Variant:** one retained generated candidate for one target. It has an
  immutable ID, a user-facing label, a selected profile, a resolved settings
  snapshot, cache identities, status, and artifacts.
- **Selected variant:** the user's preferred generated candidate for a target.
  Selection does not delete alternatives.
- **Working copy:** a user-owned REAPER copy created by
  `vgt_working_copy.lua`. It is outside the sidecar transcription cache and
  survives future vgt applies.

Labels are descriptive and editable. They are not filesystem identities.
Renaming `clean` to `clean chords` must not move artifacts, rerun a model, or
make REAPER lose the variant.

### Initial built-in profiles

| Profile | Basic Pitch detection | Cleanup | Intended use |
| --- | --- | --- | --- |
| `guitar-acoustic-detail` | Acoustic tuned defaults: onset `0.60`, frame `0.65`, minimum note `100 ms`, `80–1200 Hz`, melodia off | `merge_fragments`, `clamp_sustain`; no note-dropping stages | Preserve possible notes for listening and manual review |
| `guitar-acoustic-clean` | Same detector settings as detail | Full canonical pipeline: merge → deblip → clamp → spectrum-confirmed ghost drop → six-voice cap | Readable chord and note-shape reference |
| `guitar-acoustic-strict-chords` | Onset `0.70`, frame `0.70`, minimum note `125 ms`, `80–1200 Hz`, melodia off | Full canonical cleanup | Sparse chord-oriented reference; explicitly warns that quiet/passing notes may be lost |

`guitar-acoustic-clean` becomes the descriptive name for today's
`guitar-acoustic` behavior. Keep `guitar-acoustic` as a compatibility alias so
existing sidecars retain the same resolved settings and cache behavior.

The first release should keep existing `default`, `guitar`, `bass`,
`bass-monophonic`, and `vocals` profiles. DrumScript remains the fixed backend
for `drums`; Basic Pitch remains the backend for every other target.

### Why detail and clean share detection

Basic Pitch inference is the expensive portion. The acoustic findings show
that the old generic detector settings are not a useful "raw" comparison:
they produced multi-minute drones and pervasive impossible polyphony. Detail
therefore uses the measured acoustic detector settings, not the broken
shipping-era baseline.

Detail and clean differ only in post-processing. This makes their comparison
interpretable and allows both to derive from one Basic Pitch run. Strict-chords
changes detector thresholds and therefore requires a second inference.

## Declarative profile definitions

### Sources and precedence

Profiles resolve through:

```text
shipped built-in defaults
    → inherited profile
    → project-local profile overrides
    → optional one-run CLI overrides
    → fully resolved settings snapshot
```

The first implementation should support:

1. built-in profiles registered in `src/vgt/transcribe.py`; and
2. project-local profiles in `<project>.vgt-profiles.toml`.

A global personal profile library can be considered later. Project-local
definitions are portable with the REAPER project and make experiment settings
reviewable without hand-editing the `.vgt` state file.

### TOML shape

```toml
schema_version = 1

[profiles.my-clean-guitar]
target = "guitar"
extends = "guitar-acoustic-clean"
description = "Clean chord-oriented acoustic reference"

[profiles.my-clean-guitar.detection]
onset_threshold = 0.65
frame_threshold = 0.70
minimum_note_length_ms = 125
minimum_frequency_hz = 80
maximum_frequency_hz = 1200
melodia_trick = false
multiple_pitch_bends = false

[profiles.my-clean-guitar.cleanup.merge_fragments]
enabled = true
max_gap_s = 0.030

[profiles.my-clean-guitar.cleanup.drop_isolated_notes]
enabled = true
max_duration_s = 0.150
neighbour_window_s = 1.0

[profiles.my-clean-guitar.cleanup.clamp_sustain]
enabled = true
max_bars = 2.0

[profiles.my-clean-guitar.cleanup.drop_harmonic_ghosts]
enabled = true
intervals = [12, 19, 24, 28, 31, 36]
onset_tolerance_s = 0.050
overlap_fraction = 0.60
velocity_slack = 4
spectral_n_fft = 4096
spectral_hop_length = 512
spectral_max_harmonic_order = 8
spectral_freq_tolerance_semitones = 0.5
spectral_independent_energy_ratio = 1.5

[profiles.my-clean-guitar.cleanup.cap_simultaneous_voices]
enabled = true
max_voices = 6
min_duration_after_cap_s = 0.040
```

The project file stores overrides, not a duplicate of every inherited value.
Every created/refreshed variant stores the fully resolved settings snapshot in
the sidecar.

### Validation

Reject a profile before invoking a backend when:

- its name, target, or parent is unknown;
- inheritance contains a cycle;
- a profile is selected for an incompatible target;
- it contains Basic Pitch settings for DrumScript, or vice versa;
- onset/frame thresholds are outside `0.0–1.0`;
- duration, tolerance, ratio, FFT, hop, or frequency values are non-positive;
- minimum frequency is not below maximum frequency;
- overlap fraction is outside `0.0–1.0`;
- maximum voices or harmonic order is below one; or
- it names an unsupported cleanup stage or parameter.

Unknown keys are errors rather than silently ignored typos.

### Cleanup order

The acoustic cleanup order is load-bearing, as demonstrated in
`docs/guitar-transcription-findings.md`. In the first release, a profile may
enable, disable, and configure stages but may not reorder them. Enabled stages
always execute in canonical order:

```text
merge_fragments
drop_isolated_notes
clamp_sustain
drop_harmonic_ghosts
cap_simultaneous_voices
```

An arbitrary ordered pipeline can be considered later as an explicitly
experimental capability.

### Settings identity

Every value capable of changing output belongs in the resolved settings and
therefore in a hash. This includes the spectral STFT size and hop length, not
only the ghost gate's decision thresholds.

The variant records:

- requested profile name;
- project-profile definition hash;
- fully resolved detection settings;
- fully resolved cleanup stages and parameters;
- backend package/runtime identity; and
- the resulting detection and cleanup hashes.

Editing a profile should invalidate only variants whose resolved settings
changed.

## Two-level cache

### Layer 1: raw detection

Basic Pitch currently parses its generated CSV, applies cleanup, and rewrites
both CSV and MIDI. To derive several cleanup variants efficiently, retain a
canonical raw note-event artifact before cleanup.

A detection entry is identified by:

```text
target source content hash
+ backend/package/runtime/serialization identity
+ all Basic Pitch detection settings
+ MIDI tempo metadata when it affects emitted artifacts
= detection_hash
```

Suggested artifact layout:

```text
transcription/cache/basic-pitch/<detection_hash>/raw.csv
transcription/cache/basic-pitch/<detection_hash>/raw.mid
```

The raw CSV is authoritative for Basic Pitch derivation. Raw MIDI is optional
if it is always deterministic and cheap to regenerate from the CSV, but keeping
both makes diagnostics easier and preserves the exact backend result.

DrumScript does not initially need a raw/derived split because no alternative
drum cleanup profiles are proposed. Its implementation should still use the
same variant data model so future drum profiles do not require another schema
change.

### Layer 2: derived variant

A derived variant is identified by:

```text
raw note-event content hash
+ source-audio content hash
+ ordered cleanup settings
= cleanup_hash
```

The source-audio hash is included because acoustic ghost confirmation reads the
actual waveform and computes a spectrum. Cleanup-only variants then derive
without rerunning Basic Pitch, but are correctly invalidated when either raw
notes, source audio, or cleanup settings change.

Suggested artifact layout:

```text
transcription/guitar/<variant-id>.csv
transcription/guitar/<variant-id>.mid
transcription/drums/<variant-id>.json
transcription/drums/<variant-id>.mid
```

Variant IDs are opaque, filesystem-safe identifiers generated once. Labels
never appear in paths.

### Execution behavior

- Reconcile variants independently.
- Group Basic Pitch variants by `detection_hash`.
- Run Basic Pitch once for each uncached detection group.
- Derive every stale cleanup variant in that group from the shared raw CSV.
- Compute the source spectrogram lazily, once per source/STFT configuration,
  and reuse it across variants needing ghost confirmation.
- Persist each completed/error variant immediately so a later failure does not
  roll back earlier work.
- A missing source produces a retained per-variant
  `skipped-missing-source` state.
- Failure of one detection group or derived variant does not prevent other
  groups/targets from completing.

## Sidecar schema

Introduce schema v13 and change the transcription block to:

```json
{
  "requested_targets": ["guitar", "bass"],
  "targets": {
    "guitar": {
      "selected_variant_id": "7b3e19a4",
      "variant_order": ["7b3e19a4", "9a24ce10"],
      "variants": {
        "7b3e19a4": {
          "label": "clean",
          "requested_profile": "guitar-acoustic-clean",
          "profile_definition_hash": "...",
          "effective_profile": "guitar-acoustic-clean",
          "backend": "basic-pitch",
          "package_pin": "basic-pitch[onnx]==0.4.0",
          "serialization": "onnx",
          "source_role": "guitar",
          "input_hash": "...",
          "detection_hash": "...",
          "raw_notes_hash": "...",
          "cleanup_hash": "...",
          "settings_hash": "...",
          "status": "transcribed",
          "midi_file": "transcription/guitar/7b3e19a4.mid",
          "notes_file": "transcription/guitar/7b3e19a4.csv",
          "events_file": null,
          "note_count": 443,
          "pitch_range_midi": [40, 76],
          "first_note_s": 0.42,
          "last_note_s": 178.9,
          "midi_tempo": 120.0,
          "resolved_settings": {
            "detection": {},
            "cleanup": []
          },
          "transcribed_at": "...",
          "error": null
        }
      },
      "discarded_variants": []
    }
  },
  "detection_cache": {
    "<detection_hash>": {
      "target": "guitar",
      "input_hash": "...",
      "raw_midi_file": "transcription/cache/basic-pitch/<hash>/raw.mid",
      "raw_notes_file": "transcription/cache/basic-pitch/<hash>/raw.csv",
      "raw_notes_hash": "...",
      "created_at": "..."
    }
  }
}
```

`variant_order` controls stable status and REAPER presentation. Do not depend
on JSON object ordering.

`discarded_variants` is a compact optional audit list containing immutable ID,
label, requested profile, resolved settings/hashes, summary metrics, and
discard time, but no live artifact paths. This allows an experiment recipe to
be reconstructed without retaining all files. Bound its length or offer a
separate purge command if long-term sidecar growth becomes material.

### Migration

For every existing `analysis.transcription.targets[target]` record:

1. Create a deterministic legacy variant ID derived from the target and
   existing settings hash, avoiding a different ID on repeated upgrades.
2. Move the existing target record under `targets[target].variants[id]`.
3. Set label `default`, `variant_order: [id]`, and
   `selected_variant_id: id` for successfully transcribed records.
4. Preserve existing artifact paths (`transcription/guitar.mid`) as legacy
   paths until that variant is next recomputed. Do not require file moves
   during sidecar upgrade.
5. When recomputed, write the new variant-scoped path and remove the old
   artifact only after the new pair is committed.
6. Map the old `modes[target]` selection to the migrated variant's
   `requested_profile`; remove the global one-mode-per-target constraint only
   after all consumers use variants.

Upgrade must be idempotent and must preserve an intentionally empty requested
target set.

## CLI

Retain `vgt analyze` as the operation that performs transcription. Add a
focused `vgt transcription` command group for persistent variant management:

```sh
# List built-in and project profiles.
vgt transcription profile list "Song.RPP"
vgt transcription profile show guitar-acoustic-clean "Song.RPP"
vgt transcription profile validate "Song.RPP"

# Create retained candidates.
vgt transcription variant add guitar \
  --name detail --profile guitar-acoustic-detail "Song.RPP"
vgt transcription variant add guitar \
  --name clean --profile guitar-acoustic-clean "Song.RPP"

# Rename without recomputation.
vgt transcription variant rename guitar clean --name "clean chords" "Song.RPP"

# Select a preferred generated candidate.
vgt transcription variant select guitar clean "Song.RPP"

# Explicitly discard a rejected candidate and its generated artifacts.
vgt transcription variant discard guitar detail "Song.RPP"

# Remove archived recipe metadata as a separate deliberate operation.
vgt transcription variant purge-discarded guitar "Song.RPP"
```

Commands may accept an unambiguous variant label or immutable ID. Duplicate
labels within one target are rejected.

`variant add` persists configuration and then runs/reconciles that variant by
default. A `--defer` option may persist without running if useful, but is not
required for the MVP.

Existing commands remain compatible:

- `--transcribe <target>` creates or retains one default variant if the target
  has none.
- `--transcribe-only <target>` operates on the selected/default variant and
  remains a one-run override.
- `--mode <target>=<profile>` updates the selected/default variant during a
  deprecation period; new workflows use variant-level `--profile`.
- `--forget-transcription <target>` explicitly discards every generated
  variant for that target after a clear confirmation/error boundary. It
  remains distinct from discarding one variant.
- `--no-transcribe` skips all transcription work without changing requested
  variants.
- `--force` recomputes selected targets/variants locally without spending
  LALAL credits.

Do not expose arbitrary numeric `--set` overrides in the first release.
Project TOML provides an auditable advanced interface without making every CLI
invocation irreproducible. A later one-run override can use repeated
`--set section.key=value` and must be included in the resolved snapshot/hash.

## Status output

Human-readable status:

```text
transcription: 2 targets, 3 retained variants
  guitar
    * clean   443 notes, MIDI 40-76, guitar-acoustic-clean
      detail  1060 notes, MIDI 40-88, guitar-acoustic-detail
  bass
    * default 311 notes, MIDI 28-52, bass
```

`*` marks the selected variant. Include error and missing-source states per
variant.

JSON status exposes immutable IDs, labels, selected state, requested/effective
profiles, hashes, metrics, timestamps, and artifact paths. It remains
read-only.

Useful comparison metrics should include at least note count, pitch range,
first/last note, maximum note duration, and maximum simultaneous voices.
Acoustic diagnostics already computed by the standalone probe (fragment count,
ghost share, and chord-tone metrics) can be added later, but should not block
the variant foundation. Chord-tone agreement must retain the documented
onset-vs-time distinction.

## REAPER integration

Import every successfully transcribed retained variant immediately after its
source stem, in `variant_order`:

```text
[vgt] Guitar
[vgt] Guitar Ref — Detail (MIDI)
[vgt] Guitar Ref — Clean (MIDI)
[vgt] Guitar Ref — Strict Chords (MIDI)
```

For `original` or a target whose source stem did not import, append variants
after the stem block using the existing orphan behavior.

Each variant track:

- is `[vgt]`-managed and registered by GUID/managed mark;
- is recreated idempotently on apply;
- is unmuted but silent without an instrument;
- uses a time-based MIDI item at `reference_start`;
- validates the exact recorded variant path inside the artifact namespace;
- has a deterministic label derived from target display name plus variant
  label; and
- may visually mark the selected candidate, for example by a documented track
  color, without making color part of ownership.

Generated tracks remain read-only references. The shipped
`vgt_working_copy.lua` action is the supported transition to manual work:

1. compare generated candidates;
2. select the preferred `[vgt]` variant track;
3. create `[work] <name>` with the working-copy action;
4. edit/rename the user-owned copy freely; and
5. discard rejected generated variants when desired.

Never synchronize edited working-copy MIDI into the transcription cache.
Generated analysis and user-authored MIDI have different ownership,
reproducibility, and deletion semantics.

## Selection and deletion semantics

Selection:

- is per target;
- never changes artifacts;
- never deletes another variant;
- is preserved across analyze/apply;
- must reference a retained variant; and
- may be null if every variant is missing/error or the user clears it.

Discarding one variant:

1. Resolve the exact target and immutable variant ID.
2. Refuse ambiguity and reject a nonexistent target/variant clearly.
3. Delete only artifact files recorded by that variant and proven to be inside
   its expected namespace.
4. Remove it from `variant_order` and the live `variants` index.
5. If selected, either require `--select <replacement>` or clear selection
   explicitly; do not silently choose a winner.
6. Add its small settings/metrics record to `discarded_variants`.
7. Garbage-collect a raw detection entry only when no live variant references
   it.
8. Let the next REAPER apply remove its managed track through normal
   reconciliation.

User-owned `[work]` tracks are never inspected or deleted by these operations.

Raw cache garbage collection must use sidecar references and exact recorded
paths, never a broad directory glob. A cache entry shared by another target or
variant survives.

## Automatic chord analysis boundary

The transcription candidates are visual/listening/manual-editing references.
They do not become inputs to automatic chord detection in this feature.

The shipped chord detector already analyzes chroma from the original mix and
available instrumental/guitar/backing stems. Past experiments found stem
fusion improved exact/root chord agreement, while guitar MIDI remains an
imperfect draft. Feeding selected MIDI back into chord detection would be a
separate experiment with separate correctness and cache implications.

## Verification strategy

### Unit tests

- Profile TOML parsing, inheritance, precedence, unknown keys, cycles, bounds,
  target compatibility, and canonical cleanup order.
- Built-in detail/clean profiles share a detection hash and differ in cleanup
  hash.
- Strict-chords has a different detection hash.
- Every output-changing setting, including STFT size/hop, changes the
  appropriate hash.
- Raw detection is executed once for several cleanup variants.
- Changing one cleanup profile recomputes only dependent variants.
- Changing detection settings reruns only that detection group and dependent
  variants.
- Source changes invalidate raw detection and spectrum-dependent cleanup.
- Per-variant error isolation and missing-source recovery.
- Deterministic schema migration and preservation of legacy artifact paths.
- Exact safe deletion and reference-counted raw cache garbage collection.
- Rename and select operations do not rerun transcription.

### CLI/status tests

- Add, rename, select, discard, and purge lifecycle.
- Duplicate-label and ambiguous-reference rejection.
- Compatibility behavior of existing transcription flags.
- Human and JSON multi-variant status ordering and selection marker.
- Project profile validation errors occur before backend invocation.

### ReaScript tests

- Every retained successful variant imports once in stable order after its
  stem.
- Selected visual state is deterministic.
- Missing/error variants create no track.
- Orphan target variants remain supported.
- Variant paths cannot escape their expected namespace.
- Re-apply removes a discarded variant without touching retained variants or
  `[work]` tracks.
- Working-copy action produces a user-owned editable copy of a selected
  variant with no managed ownership mark.

### Goal contract

Extend the offline end-to-end contract to create two fake guitar variants that
share one raw detection, apply twice, select one, create or simulate a
user-owned working copy where possible in the stubbed contract, discard the
other, apply again, and prove:

- one stem plus the expected retained variant tracks;
- no duplicates across applies;
- exact artifact/cache reuse;
- rejected artifacts disappear;
- selected state survives;
- user tracks and working copies survive; and
- no credentials, network, model downloads, or live REAPER are required.

### Human-owned verification

As required by `docs/AGENTS.md`, listening and live REAPER judgment remain
human-owned. Suggested real-project checklist:

- compare detail and clean against the same acoustic stem;
- verify clean preserves intentional octave chord shapes after the spectral
  gate;
- measure before/after `%ghost`, maximum polyphony, fragmentation, and both
  chord-tone metrics with `guitar_transcription_probe.py`;
- create a working copy from the chosen candidate and confirm it survives
  analyze/apply; and
- discard the rejected candidate and confirm only its generated track/artifacts
  disappear.

The round-three spectral gate has synthetic coverage but has not yet been
re-measured on the real `7Rivers` stem. That limitation should remain visible
and must not be presented as completed automated evidence.

## Implementation sequence

### A. Profile definitions and schema foundation

- Add built-in detail/clean/strict profiles and compatibility aliasing.
- Parse and validate `<project>.vgt-profiles.toml`.
- Resolve inherited profiles to immutable settings snapshots.
- Add schema v13 target/variant records and deterministic migration.
- Make all output-changing spectral parameters hash-visible.

This is the foundation for every later issue.

### B. Raw detection cache and derived variants

- Split Basic Pitch backend output capture from cleanup derivation.
- Add detection grouping/cache artifacts and reference tracking.
- Reconcile several variants independently with immediate durable commits.
- Reuse one source spectrogram where applicable.
- Implement safe cache garbage collection.

Depends on A.

### C. Variant lifecycle CLI and status

- Add profile list/show/validate.
- Add variant add/rename/select/discard/purge operations.
- Preserve compatibility flags and document deprecation behavior.
- Add multi-variant human/JSON status and comparison metrics.

Depends on A and B.

### D. REAPER multi-variant import

- Validate and import variant-scoped artifact paths.
- Place all retained variants after their source stem in stable order.
- Represent selected state visually.
- Reconcile discarded variants idempotently.
- Verify working copies remain user-owned and untouched.

Depends on A; can proceed in parallel with most of B once the schema and
artifact path contract are fixed.

### E. Integration, migration, documentation, and goal contract

- Extend the offline goal contract.
- Add compatibility/migration regression coverage.
- Update the user manual, goal, status examples, and profile authoring guide.
- Add an opt-in saved-project verifier if static/stubbed tests leave a gap.
- Record the human verification checklist without turning it into an
  autonomous issue acceptance criterion.

Depends on B, C, and D.

## Acceptance criteria

The feature is complete when:

1. A project can retain at least two guitar variants with different profiles.
2. Detail and clean share one cached Basic Pitch detection.
3. Variants have immutable IDs, editable unique labels, independent status,
   settings snapshots, hashes, and artifacts.
4. Profile changes invalidate only affected detection/cleanup work.
5. Every retained successful variant appears once beside its source stem.
6. Selection is persistent and non-destructive.
7. Discard removes only the exact generated variant and unreferenced cache
   data.
8. A user can create an editable working copy that survives variant
   reconciliation.
9. Existing single-target sidecars migrate without losing artifacts or
   changing user-owned REAPER objects.
10. Existing transcription flags remain compatible during the documented
    transition.
11. The offline unit/CLI/ReaScript/goal-contract suite passes without network,
    credentials, model downloads, or live REAPER.
12. Documentation clearly distinguishes generated variants, selected variants,
    working copies, and automatic chord analysis.

## Risks and boundaries

- **Combinatorial UI:** profiles, rather than dozens of CLI flags, are the
  primary interface. Arbitrary numeric overrides are deferred.
- **Misleading comparison:** candidates that differ only in cleanup should
  share raw detection so the difference is attributable. The UI/status should
  expose requested/effective profile names and hashes.
- **Cache growth:** generated MIDI/CSV files are small, but raw detections can
  accumulate. Reference-counted explicit garbage collection is required.
- **False confidence:** clean is easier to read, not ground truth. The latest
  ghost gate is conservative but lacks real-stem remeasurement.
- **Profile drift:** every variant stores its resolved settings and definition
  hash, so a renamed or edited profile cannot silently masquerade as the old
  run.
- **Migration safety:** schema upgrade must not require live REAPER or mutate
  the RPP; legacy artifacts remain valid until an atomic replacement succeeds.
- **Manual edits:** there is still no MIDI `vgt sync`. Working copies are the
  explicit supported editing boundary.
- **Backend scope:** no new transcription model, tablature/string assignment,
  performance scoring, or MIDI-to-chord feedback is included.
