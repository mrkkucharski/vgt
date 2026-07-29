# Stem transcription with Basic Pitch — implementation plan

Status: accepted · Date: 2026-07-21
Basis: the "Automatic music transcription" and "Building a virtual guitar teacher" research pages in the `llm-wiki` vault (`topics/music-ai/wiki/`), and the delivered vgt baseline in [USER-MANUAL.md](USER-MANUAL.md).

This plan is the design of record for the transcription capability; the issues T-A…T-F below are tracked as GitHub issues. It was drafted in the `llm-wiki` repo and copied here so it travels with the code.

Decision taken up front: **Basic Pitch** (Spotify, Apache-2.0) is the transcriber. This capability jumps ahead of the then-planned practice-workflow phase and delivers a *reference transcription* instead — the "draft guitar reference as a MIDI track" item that was cut from phase 1.

## Goal

After `vgt analyze`, the project gains a machine transcription of the **isolated guitar stem** as MIDI with pitch bends, imported by the existing ReaScript action as a `[vgt] Guitar Ref (MIDI)` track sitting directly beneath the guitar stem it was transcribed from, aligned to the same beat grid and the same `reference_start` offset as every other vgt object.

Guitar is only the default target. Transcription is **multi-target**: any separated stem can be requested, several can be kept side by side, and every kept transcription is its own cached entry, its own artifact, and its own `[vgt] … Ref (MIDI)` track. Bass and vocals are the obvious second and third choices — a bass line is what you check a rhythm part against, and a vocal melody is the most useful thing to have on a grid when arranging.

**In scope:** a multi-target `transcription` analysis stage, a `Transcriber` backend seam with a Basic Pitch backend and a fake backend, per-target MIDI/CSV artifacts, ReaScript import of every kept target, `vgt status` reporting, docs.

**Out of scope (explicitly deferred):** tablature / string-fret assignment (the wiki's "miss-fretting" failure mode makes automatic tab a draft generator at best), performance scoring, reading edited MIDI back into the sidecar via `vgt sync`.

## Why this fits the current architecture

vgt already has every piece this needs, and the plan reuses them rather than inventing parallel machinery:

| Existing mechanism | Reused as |
|---|---|
| `sidecar.ANALYSIS_STAGES` + `refresh_stage` input/settings-hash caching | new `transcription` stage, with one cached entry **per target**, recomputed only when that stem or its settings change |
| `analysis.stems.artifacts` — a per-name index of records with `file`/`sha256`/timestamps | `analysis.transcription.value.targets` — the same shape, one record per transcribed stem |
| `analysis.stems.optional_stems` — a persisted set of opt-in requests that survives retries | `requested_targets` — the set of stems you want kept, persisted the same way |
| `vgt/<namespace>/` artifact namespace | `transcription/<target>.mid` + `transcription/<target>.csv` beside `chords.txt`, `sections.txt`, `tempo-click.wav` |
| `Separator` seam + `FakeSeparator` (separation.py) | `Transcriber` seam + `FakeTranscriber`, so the offline suite never runs a model |
| `add_stem_tracks` in `vgt_initialize.lua` — loops a fixed track table, validates each path inside the namespace, skips missing records without failing | extended in place: each stem is followed by its own `[vgt] … Ref (MIDI)` track via `add_reference_midi_track` |
| Analysis stays in the Python CLI, mutation stays in REAPER | unchanged — no MIDI is written by text-editing the RPP |

## The one hard constraint: Basic Pitch cannot run in vgt's interpreter

Verified today, not assumed:

- Latest release is **basic-pitch 0.4.0 (Aug 2024)**; the project ships tf / coreml / tflite / onnx runtimes.
- `uv pip install --dry-run "basic-pitch[onnx]"` **resolves on Python 3.11 and 3.12**, and **fails on 3.13 and 3.14** (its TensorFlow-family markers have no wheels there). vgt's venv is **Python 3.14.2**.
- The 3.11 resolution pulls **numpy 2.4**, while vgt pins `numpy>=1.22,<2` for madmom.
- resampy 0.4.2 imports `pkg_resources`, so the env also needs `setuptools<81` — the same workaround vgt already applies to madmom.

So Basic Pitch **must not be a vgt dependency**, not even an optional extras group like `madmom`/`msaf`. It runs as an isolated subprocess:

```sh
uvx --python 3.11 --with "setuptools<81" --from "basic-pitch[onnx]==0.4.0" \
    basic-pitch <outdir> <guitar-stem.wav> \
    --model-serialization onnx --save-midi --save-note-events \
    --midi-tempo <detected bpm> --minimum-note-length <ms> \
    --minimum-frequency <hz> --maximum-frequency <hz>
```

Proven end-to-end today on the fixture audio (`test/Reaper Project/Media/Paris Metro Punk.mp3`): cold env build ≈ 35 s (cached afterwards), transcription of a ~3-minute track ≈ 10 s CPU, producing a `.mid` and an 872-note `.csv`. No GPU, no credits, no network after the first env build.

Notes that follow from this:

- **Pin the version** (`==0.4.0`) in the invocation. It makes runs reproducible and gives the settings hash something honest to key on; it also protects against an unmaintained upstream moving under us.
- **Force `--model-serialization onnx`.** The onnx extra still installs `coremltools` on macOS, and Basic Pitch's default preference order would silently pick CoreML — different runtime, different numbers, same command line.
- Allow `VGT_BASIC_PITCH_CMD` to override the whole invocation for a pre-installed `uv tool install`ed binary, so an offline machine can prebuild the env once.

## Design

### 1. New module `src/vgt/transcribe.py`

```python
class TranscriptionError(ValueError): ...

@dataclass(frozen=True)
class TranscriptionSpec:      # everything that changes one target's output
    backend: str              # "basic-pitch" | "fake"
    package_pin: str          # "basic-pitch[onnx]==0.4.0"
    serialization: str        # "onnx"
    onset_threshold: float
    frame_threshold: float
    minimum_note_length_ms: float
    minimum_frequency_hz: float
    maximum_frequency_hz: float
    multiple_pitch_bends: bool
    melodia_trick: bool
    midi_tempo: float | None  # from the tempo stage

class Transcriber(Protocol):
    def transcribe(self, source: Path, destination_dir: Path, spec: TranscriptionSpec,
                   progress: Callable[[str], None]) -> TranscriptionResult: ...
```

`BasicPitchTranscriber` builds and runs the `uvx` command per target, then moves/renames the outputs to the stable names `transcription/<target>.mid` and `transcription/<target>.csv` (Basic Pitch names files after the input, and two stems could otherwise collide). `FakeTranscriber` writes a tiny deterministic MIDI file, so the whole pipeline including the ReaScript import is testable offline.

**One spec per target.** The spec is derived from a per-stem default table plus any user overrides, and it is what the target's `settings_hash` covers — so retuning guitar thresholds never invalidates a kept bass transcription:

| Target | min/max frequency | Rationale |
|---|---|---|
| `guitar` | 70 – 1400 Hz | below standard E2 = 82.4 Hz so drop-D and Eb tunings survive; above the 24th-fret E6 = 1318.5 Hz |
| `bass` | 30 – 400 Hz | 5-string low B = 30.9 Hz; nothing musical above the dusty end of the neck |
| `vocals` | 70 – 1200 Hz | bass voice to whistle-adjacent soprano |
| `piano` / `strings` / `instrumental` / `backing` / `original` | Basic Pitch defaults (full range) | polyphonic and unpredictable; no defensible narrowing |

Shared defaults across targets: `minimum_note_length ≈ 60 ms`, Basic Pitch's default onset/frame thresholds, melodia trick on, single pitch-bend mode (multi-bend splits each pitch onto its own MIDI instrument, which would clutter the REAPER take).

**CSV quirk to handle:** the note-events CSV's header is `start_time_s,end_time_s,pitch_midi,velocity,pitch_bend`, but `pitch_bend` is a *variable-length trailing sequence* of values — rows have differing column counts. Parse the first four fields and treat the remainder as the bend series; never hand this file to a strict CSV reader.

### 2. Targets: which stems get transcribed, and which are kept

Basic Pitch's own guidance and the wiki both say: one instrument at a time. That is why a target is always a **single named source**, never a merged set — and why several targets means several independent runs, not one combined pass.

Valid target names are exactly the separation artifact names plus the mix: `guitar`, `bass`, `vocals`, `drums`, `instrumental`, `backing`, `strings`, `piano`, `original`.

- **Default requested set: `{guitar}`.** Unchanged behaviour for anyone who never touches the new flag.
- **`--transcribe <target>` (repeatable) adds targets to the persisted requested set**, exactly as `--extra-stem` persists opt-in separation requests. `vgt analyze --transcribe bass --transcribe vocals` once is enough; every later run keeps refreshing guitar, bass, and vocals without re-stating them.
- **`--forget-transcription <target>`** removes a target from the set and deletes its artifacts — the only way a kept transcription goes away. Nothing is dropped implicitly just because a run didn't mention it.
- **`--transcribe-only <target>`** transcribes one target this run without touching the persisted set (for tuning thresholds on a single stem).

Each requested target resolves to a source independently, through the existing `separation.artifact_path` + on-disk check (the same defensive treatment `chord_sources` gives optional artifacts):

1. A target whose stem artifact exists is transcribed (or served from cache).
2. A target whose stem is **missing is skipped**, with a per-target progress message ("transcription skipped for bass: no bass stem available"), and the run continues with the targets that do resolve. A missing guitar stem is no longer a special case — it is just the default target failing to resolve. Nothing silently falls back to the mix: transcribing the full mix and labelling it a guitar reference is worse than producing nothing.
3. `original` is a legitimate explicit target (`--transcribe original`) for the curious, recorded as its own entry so nobody later mistakes it for an isolated part.
4. `drums` is accepted but warned about once — Basic Pitch is a pitch model, and a drum transcription is nonsense-shaped output, not a groove map.

Never trigger paid separation to satisfy transcription: the stage consumes whatever separation already produced, exactly like chord fusion does. If a stem arrives later, the next `vgt analyze` picks up the still-requested target and fills it in.

### 3. Sidecar: schema v9

Append `"transcription"` to `ANALYSIS_STAGES` (after `chords`, since it reads the tempo stage's BPM). It is a **plain stage** — no `detected`/`value` split, because there is no read-back path for MIDI edits (see deferred work).

Because targets are independent, the stage cannot use the stage-level `input_hash`/`settings_hash` pair the single-value stages use: one changed stem must not invalidate the others. It follows the `stems` block's precedent instead — the stage owns an index whose **entries carry their own hash pair**, and `analysis.py` reconciles them target by target (`_refresh_target`, a small sibling of `_refresh_stage_with_detected`). Stage `value`:

```json
{
  "requested_targets": ["guitar", "bass", "vocals"],
  "targets": {
    "guitar": {
      "backend": "basic-pitch",
      "package_pin": "basic-pitch[onnx]==0.4.0",
      "serialization": "onnx",
      "source_role": "guitar",
      "input_hash": "…",           // that stem's sha256
      "settings_hash": "…",        // that target's TranscriptionSpec
      "status": "transcribed",     // | "skipped-missing-source" | "error"
      "midi_file": "transcription/guitar.mid",
      "notes_file": "transcription/guitar.csv",
      "note_count": 872,
      "pitch_range_midi": [40, 76],
      "first_note_s": 0.42,
      "last_note_s": 178.9,
      "midi_tempo": 118.02,
      "settings": { "onset_threshold": 0.5, "…": "…" },
      "transcribed_at": "…",
      "error": null
    },
    "bass": { "…": "…" }
  }
}
```

Per-entry `input_hash` is the stem artifact's recorded `sha256` when present (content-addressed and already computed by separation), falling back to `hash_source_file`; `settings_hash` covers that target's full `TranscriptionSpec` including the package pin. A `skipped-missing-source` entry is retained, not deleted — it is the record of a still-wanted target whose stem hasn't arrived yet.

Schema v9 adds the stage with `requested_targets: ["guitar"]` and an empty `targets` index; older sidecars need no data migration.

### 4. CLI wiring

Inside `vgt analyze`, transcription runs in the **same second `analyze(..., stages=("chords", "transcription"))` call** that currently decodes chords after separation — that is the point where stems are known to exist.

New flags:

- `--transcribe <target>` (repeatable) — add a target to the persisted requested set.
- `--forget-transcription <target>` (repeatable) — drop a target and delete its artifacts.
- `--transcribe-only <target>` — run just this target now, leaving the persisted set alone.
- `--no-transcribe` — skip the stage entirely this run (mirrors `--no-stems`); the requested set is untouched.
- `--force` already recomputes every resolvable target; because the work is local and free, it needs none of the `--force-stems`/`--accept-stem-cost` cost machinery, and it takes no separation lease.

Targets run sequentially with per-target progress lines. **Failure is per target**: a missing `uvx`, a non-zero exit, or malformed output marks that entry `error` and the remaining targets still run — the established "optional capability degrades, analysis still succeeds" behaviour of separation, applied one level down.

`vgt status` gains a block rather than a line:

```
transcription (basic-pitch 0.4.0): 3 requested
  guitar   872 notes, MIDI 40-76, transcribed 2026-07-21T…
  bass     311 notes, MIDI 28-52, transcribed 2026-07-21T…
  vocals   skipped — no vocals stem available
```

with each target's artifacts listed in the artifact section.

### 5. ReaScript import

Rather than a separate block of MIDI tracks, each transcription is inserted **immediately after the stem track it came from**, inside `add_stem_tracks`'s existing loop: `[vgt] Guitar` then `[vgt] Guitar Ref (MIDI)`, `[vgt] Bass` then `[vgt] Bass Ref (MIDI)`. The reference is only ever read against its stem, so it belongs next to it — and the stem loop already walks a fixed `STEM_TRACKS` order, which gives the pairing a stable layout for free. `add_reference_midi_track(index, target, …)` is the shared helper, called from the loop and returning the new index.

- One track per target with a `status` of `transcribed`, named `[vgt] Guitar Ref (MIDI)`, `[vgt] Bass Ref (MIDI)`, … from the same target→label table the Python side uses. Entries with `skipped-missing-source` or `error` create no track and are not warnings — they are expected states.
- A transcribed target with **no imported stem track** — `original`, or a stem whose artifact went missing — has nothing to sit beside, so it is appended after the stem block in the `targets` index's own order.
- Created **unmuted**, following the `Chords`/`Beats` precedent rather than `Click`'s. A MIDI item makes no sound on its own — a track with no instrument produces silence — and muting only dims the track in the arrange view, which is exactly where these notes are meant to be read.
- Item created via `reaper.PCM_Source_CreateFromFile` on the `.mid` (REAPER imports it as an in-project MIDI item), positioned at `reference_start`, `C_BEATATTACHMODE = 0` (time-based) so a later tempo-map edit never stretches it against the audio.
- Path validated exactly like stems: the record's `midi_file` must match the expected `transcription/<target>.mid` name **inside the recorded artifact namespace**, else warn and skip.
- Each track registered in `managed_tracks` so re-apply removes and recreates them idempotently, and so they are never confused with user tracks.

Because re-apply recreates these tracks, **user edits to a `[vgt] … Ref (MIDI)` track do not survive** — the manual must say so and tell users to drag a copy onto their own track before editing. This is the honest version of the current invariant set; a real correction path is deferred work, not a silent hazard.

### 6. Tests and verification

- `tests/test_transcribe.py` — per-target spec/settings hashing (including that the per-stem frequency table gives guitar and bass different hashes), command construction (pin, serialization flag, tempo, frequency bounds), lenient CSV parsing, per-target artifact naming, missing-source skip, per-target failure isolation (one target errors, the others still complete), `FakeTranscriber` round trip.
- `tests/test_analysis.py` — stage ordering (tempo → … → transcription); **per-target cache independence**: changing the guitar stem recomputes guitar only and leaves the bass entry's `transcribed_at` untouched; a `skipped-missing-source` target fills in once its stem appears; `--force` recomputes all.
- CLI tests — `--transcribe` persists across runs, `--transcribe-only` does not, `--forget-transcription` removes the entry *and* its files, `--no-transcribe` preserves the requested set.
- `tests/test_status.py` / `tests/test_sidecar` — v9 upgrade and the new multi-target summary block.
- `tests/test_reascript.py` — the existing source-assertion + stubbed-Lua style: each ref track lands directly after its stem, one track per `transcribed` entry, no track for skipped/errored entries, and an orphan target (`original`, or a transcription whose stem artifact vanished) still lands after the stem block instead of being dropped.
- `scripts/verify_transcription_apply.py` — the opt-in saved-project proof, matching `verify_stem_apply.py`: copy the fixture, write two small valid MIDIs (guitar + bass) into the namespace, apply twice, save, and inspect the RPP for exactly two time-based, unmuted, `[vgt]`-owned MIDI tracks, each immediately following its own stem track.
- **Human-owned:** listening to the reference against the stem, and any live REAPER check, stay with the user per `docs/AGENTS.md`.

## Issue breakdown (proj-mgr)

| # | Issue | Priority | Depends on |
|---|---|---|---|
| T-A | `transcribe.py`: per-target spec + per-stem defaults table, `Transcriber` seam, `FakeTranscriber`, sidecar schema v9 `transcription` stage with the per-target `targets` index | high | — |
| T-B | `BasicPitchTranscriber`: pinned `uvx` invocation, `VGT_BASIC_PITCH_CMD` override, artifact normalization + validation, provenance | high | T-A |
| T-C | Per-target reconciliation in `analysis.py` + CLI/status wiring: `--transcribe`, `--forget-transcription`, `--transcribe-only`, `--no-transcribe`, per-target skip/degrade, multi-target status block | medium | T-A, T-B |
| T-D | ReaScript `[vgt] … Ref (MIDI)` import for every kept target + `verify_transcription_apply.py` | medium | T-A |
| T-E | Docs: USER-MANUAL table/workflow rows, GOAL "delivered capability" entry, multi-target caveats | medium | T-C, T-D |
| T-F | Quality pass on owned songs: per-stem thresholds/min-note-length tuning (guitar and bass at least), note-count sanity, written findings (human listening required) | low | T-E |

T-A and T-D can run in parallel once the artifact naming scheme and the `targets` index shape are fixed in T-A.

## Risks and honest limits

- **Upstream staleness.** basic-pitch's last release is Aug 2024 and its Python ceiling is 3.12. The subprocess seam is precisely what keeps that from infecting vgt; if the package dies, only `BasicPitchTranscriber` is replaced.
- **Quality on distorted, polyphonic guitar.** The wiki is blunt: guitar transcription is an open problem; expect a *draft*. Note count and pitch range in the sidecar give a cheap sanity signal (an implausible note count usually means the stem, not the model, is bad).
- **No fretboard information.** MIDI pitch only; string/fret assignment (and therefore tab) is deliberately not attempted.
- **Stem artifacts dominate the result.** A poor LALAL guitar stem yields a poor transcription; keep the stem audible next to the reference for A/B checking.
- **Multi-target runtime adds up linearly.** Each target is a separate model pass (~10 s CPU per 3-minute track, measured), so three kept targets is ~30 s per analyze — still free and local, but no longer negligible on a fanless M4 Air. Cache-hit runs cost nothing.
- **Cold-start cost.** The first run builds a ~44-package Python 3.11 env. Document `uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"` as the offline-prep step.

## Sources

- [Basic Pitch](https://github.com/spotify/basic-pitch) and [basic-pitch on PyPI](https://pypi.org/project/basic-pitch/) — accessed 2026-07-21
- Local verification: `uv pip install --dry-run "basic-pitch[onnx]"` on Python 3.11/3.12/3.13/3.14 and a full `uvx` transcription run against the vgt fixture audio, 2026-07-21
- `topics/music-ai/wiki/Automatic music transcription.md`, `topics/music-ai/wiki/Building a virtual guitar teacher.md`
