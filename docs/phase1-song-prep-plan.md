# Virtual Guitar Teacher — Phase 1: Song-Prep + Reference Generator

Status: proposed (rev 2, after discussion) · Date: 2026-07-18
Basis: [[Building a virtual guitar teacher]] and [[AI-assisted instrument practice and performance assessment]] in `topics/music-ai/wiki/` (and the two retained deep-research PDFs in `topics/music-ai/source/`).

Decisions from discussion: LALAL.AI (active subscription) is the default separator with Demucs as offline fallback; target machine is an M4 MacBook Air (16 GB, fanless); the primary workflow starts from a song already loaded in an **existing REAPER project**, so the importer augments the open project rather than generating a fresh one.

## Goal

Starting from a song item in an open REAPER project (or a bare audio file), produce in that project:

- separated stems on named `[vgt]` tracks (guitar isolated; backing = bass/drums/vocals/other),
- a tempo map and downbeat grid (only where the project doesn't already have one — see the tempo-map rule below),
- song sections as `[vgt]` REAPER regions (intro/verse/chorus/…),
- detected key and beat-aligned chords as text items on a muted "[vgt] Chords" track,
- a draft guitar reference as a muted MIDI track (with pitch bends),
- a record-armed "MY TAKE" track,
- a machine-readable `manifest.json` recording every tool, version, setting, and human correction.

**Non-goals for phase 1** (explicitly deferred): performance scoring, practice UI/looping logic (phase 2), tempo-ramped renders, tab generation (deprioritized in the source research as too error-prone to be the first investment), any hard cloud dependency — the default path uses LALAL.AI, but the tool must remain fully **offline-capable** via the Demucs fallback.

## Where the code lives

New sibling repository (suggested: `~/projects/guitar-teacher`, working name **vgt**). This repo (`llm-wiki`) is a knowledge vault with its own proj-mgr goal; only the plan lives here. The wiki remains the research reference.

## Architecture

Two components, cleanly split — this mirrors the "preparation may be heavy/offline, practice must be local/deterministic" principle from the research. REAPER is the driver: the workflow starts and ends inside the user's open project.

```
┌─────────────────────────────────┐  launches   ┌────────────────────────────┐
│  REAPER (user's open project)   │  detached   │  vgt CLI (Python 3.11)     │
│                                 │────────────▶│  staged pipeline:          │
│  vgt_analyze.lua                │             │  ingest → separate (LALAL/ │
│   • selected item → source path │             │  Demucs) → beats → key →   │
│   • records position offset     │             │  chords → sections →       │
│                                 │             │  reference → package       │
│  vgt_apply.lua                  │             └──────────┬─────────────────┘
│   • watches for manifest.json   │   writes             │
│   • augments project (additive, │◀─────────────────────┘
│     idempotent, offset-shifted) │        song folder: stems/ midi/
└─────────────────────────────────┘        analysis/ manifest.json
```

Rationale:

- **Heavy ML deps (PyTorch, ONNX) stay out of the DAW process.** The CLI is a batch tool; REAPER never loads torch.
- **No export step needed.** `vgt_analyze.lua` reads the selected item's *source file path* via the REAPER API and feeds it to the pipeline directly, recording the item's position offset for the import step. Edge case: if the item is trimmed or time-stretched, v1 refuses with a clear message ("use the full, unstretched item"); render-to-temp is the v2 fallback.
- **The project is augmented by REAPER**, via `vgt_apply.lua` reading `manifest.json` — not by writing `.RPP` text. The API route can't corrupt the project, handles tempo-map/region/item construction correctly, and the same manifest contract feeds phase 2's practice controller later.
- **Non-destructive and idempotent.** Everything vgt creates is `[vgt]`-prefixed (tracks, regions). Applying never deletes or renames anything it didn't create; re-applying first removes only its own `[vgt]` objects. All imported times are shifted by the analyzed item's position.
- **Tempo-map rule** (the one genuinely invasive write — a project has exactly one tempo map): write it only if the project still has a single default tempo marker; otherwise leave the map untouched and ask, offering a muted "[vgt] beats" marker-item track as the non-invasive alternative.
- **`manifest.json` is the contract.** Analysis is useless if it can't be corrected; every field is overridable and corrections survive re-runs.
- **Standalone mode** (`vgt prep song.mp3` → fresh project) remains as the trivial special case: empty project, insert item, analyze.

### Pipeline stages, tool choices, and fallbacks

Each stage caches its output keyed on input-hash + settings, so correcting the tempo doesn't re-run 5 minutes of Demucs.

| Stage | Primary choice | Fallback / alternative | Known risks |
|---|---|---|---|
| Ingest | ffmpeg → 44.1 kHz WAV; loudness measured with pyloudnorm | — | none significant |
| Separation | **LALAL.AI API** (default — active subscription; dedicated acoustic- and electric-guitar stems, which is the one stem that must be good; async upload → split → download, cached so each song costs credits once) | Demucs `htdemucs` 4-stem on MPS (offline fallback); `audio-separator`-style UVR/RoFormer backends later | Cloud upload in the default path (own files, personal use — fine, but noted); subscription lapse → fallback must always work; Demucs repo archived → pin version; skip `htdemucs_6s` guitar (experimental, bleeding — LALAL covers it) |
| Beats/downbeats | madmom DBN beat + downbeat trackers | librosa `beat_track` (no downbeats) | madmom's NumPy-version pinning is fragile; isolate in its own extras group |
| Key | Essentia `KeyExtractor` | librosa chroma + Krumhansl–Schmuckler template correlation (simple, ~50 lines) | Essentia wheels can be painful on macOS/Apple Silicon → treat as optional |
| Chords | madmom CNN chord recognition (maj/min vocabulary), labels then snapped to the beat grid | Chordino via sonic-annotator CLI | maj/min vocabulary won't capture 7ths/slash chords — acceptable for practice scaffolding, flagged in manifest |
| Sections | MSAF novelty/structure segmentation | librosa self-similarity novelty + peak-picking heuristic | MSAF maintenance is uncertain; labels are generic ("A", "B") — human renaming is a first-class step, not a failure mode |
| Guitar reference | Basic Pitch (ONNX) on the **isolated guitar stem**, keeping pitch bends; post-filter: min note length, confidence threshold; no key-snapping (bends matter) | — | works best on one instrument at a time — which is exactly why it runs on the separated stem |
| Package | write `manifest.json` + human-readable sidecars (`beats.txt`, `chords.lab`, `sections.txt`) | — | — |

### Tempo-map construction (the subtle part)

Naively emitting one REAPER tempo marker per detected beat produces an unusable, jittery map. Instead:

1. Fit beat intervals; if variance is below a threshold, emit a **single constant BPM + downbeat offset** (covers most practice-worthy recordings, including anything cut to a click).
2. Otherwise fit **piecewise-linear tempo spans** and emit markers only at significant changes.
3. Stem audio items are set **time-based, not beat-based**, so later tempo-map edits never stretch the audio.

The mode used (constant vs. piecewise) and residual error go into the manifest.

### Human-in-the-loop correction

Auto-analysis *will* be wrong sometimes; the design treats correction as normal operation:

- `corrections.yaml` next to the manifest: tempo/key overrides, section renames and boundary nudges, chord fixes. Re-running the pipeline re-applies overrides and marks fields `human_verified: true`.
- Every analysis stage emits a **checkable artifact**: a click-only render for the beat grid, a printable chord sheet, a section timeline. Trust is earned per song, not assumed.

### Manifest sketch

```json
{
  "schema_version": 1,
  "song":       {"title": "...", "source_sha256": "...", "duration_s": 214.6},
  "provenance": {"created": "2026-07-18", "tools": {"demucs": "4.x", "basic-pitch": "x.y"}, "settings": {}},
  "stems":      {"backend": "lalal", "model": "...", "files": {"guitar": "stems/guitar.wav", "...": "..."}},
  "reaper":     {"item_offset_s": 0.0, "source_item_guid": "..."},
  "beats":      {"mode": "constant", "bpm": 118.02, "downbeat_offset_s": 0.412,
                 "time_signature": "4/4", "beat_times": ["..."], "human_verified": false},
  "key":        {"root": "E", "scale": "minor", "confidence": 0.83},
  "chords":     [{"start": 0.41, "end": 2.44, "label": "E:min"}],
  "sections":   [{"start": 0.0, "end": 14.2, "label": "intro"}],
  "reference":  {"guitar_midi": "midi/guitar_ref.mid", "pitch_bend_range": 2},
  "overrides_applied": []
}
```

### Tracks added to the project by `vgt_apply.lua`

| Track | Content | State |
|---|---|---|
| MY TAKE | empty | record-armed, input monitoring |
| [vgt] Guitar Ref (MIDI) | `guitar_ref.mid` | muted |
| [vgt] Guitar Stem | isolated guitar audio | audible (mute during practice — phase 2 automates this) |
| [vgt] Backing: Bass / Drums / Vocals / Other | remaining stems | audible |
| [vgt] Chords | text items per chord segment | muted, locked |

Plus: `[vgt]` regions for sections, tempo map per the tempo-map rule, native metronome configured to the grid. The user's original song item/track is left untouched (they'll typically mute it in favor of the stems).

## Stack and target hardware

Python 3.11 · `uv` for env/deps · `typer` CLI · `pydantic` manifest models · ffmpeg · LALAL.AI REST API (default separator) · torch (Demucs fallback; MPS) · ONNX Runtime (Basic Pitch) · Lua ReaScript with a vendored JSON decoder for the two REAPER actions.

Target machine: **M4 MacBook Air, 16 GB unified memory** (confirmed). Two consequences: it's fanless, so sustained local separation would thermal-throttle — another reason LALAL-by-default is right; and with separation offloaded, the remaining local stages (madmom, chord CNN, Basic Pitch) are lightweight, putting expected end-to-end time at ~2–3 min/song plus upload. Fully offline operation (Demucs fallback) remains supported, just slower and warmer.

## Milestones

| # | Deliverable | Acceptance criteria | Size |
|---|---|---|---|
| M0 | Repo skeleton: CLI, config, song-folder layout, manifest schema, stage-cache framework | `vgt prep song.mp3` creates the folder + a stub manifest | S |
| M1 | Separation stage: LALAL adapter (default) + Demucs fallback behind one `Separator` interface | guitar/bass/drums/vocals(/other) stems land in the song folder from either backend; stage cached so each song costs credits once | M |
| M2 | Beat/tempo/downbeat grid + click-render verification artifact | click render sounds locked on 3 test songs, or the failure is visible and correctable via `corrections.yaml` | M |
| M3 | Key + beat-aligned chords + chord sheet artifact | chord sheet is usably close on the golden songs after ≤ a few corrections | M |
| M4 | Sections → regions + rename workflow | section boundaries within a bar or two; renaming is a one-file edit | S |
| M5 | Basic Pitch guitar reference MIDI with bends + post-filtering | reference MIDI audibly resembles the part when auditioned with a sine/guitar SF2 | S |
| M6 | `vgt_analyze.lua` + `vgt_apply.lua` against an **existing project** | end-to-end: select song item → analyze → project augmented per the table above; additive, idempotent re-apply; offset-shifted times; tempo-map rule honored; trimmed-item case refused cleanly | M |
| M7 | Golden-song evaluation pass: 3–5 songs the user knows well, spot-check protocol, findings doc | documented accuracy notes feeding phase 2 decisions | S |

Ordering rationale: the beat grid (M2) precedes chords/sections because both are expressed on the shared beat-synchronous grid — the same ordering the research recommends.

## Risks

- **Dependency fragility** (madmom pins, Essentia wheels, archived Demucs): mitigated by pinning, extras groups, and interface seams for each stage.
- **Analysis accuracy on real mixes** (chords/sections especially): mitigated by making the correction loop first-class and shipping verification artifacts, not by chasing model quality in phase 1.
- **Cloud in the default path**: LALAL API changes, credit exhaustion, or subscription lapse must degrade gracefully to Demucs — the fallback is exercised in tests, not just theoretical. Uploaded audio is the user's own, for personal practice.
- **Writing into a live user project**: the highest-consequence surface. Mitigated by the `[vgt]` prefix discipline, additive-only writes, idempotent re-apply, the tempo-map rule, and REAPER's undo block wrapping every apply.
- **Rights**: processing the user's own audio for personal practice; stems and manifests stay on-disk and are not redistributed — consistent with the provenance guidance in `Copyright, consent, and provenance in music AI`.

## Open questions

1. ~~Target machine?~~ Resolved: M4 MacBook Air, 16 GB. LALAL default; Demucs fallback on MPS.
2. REAPER version in use (importer targets 7.x APIs) — assumed 7.x, confirm.
3. First 3–5 golden songs — genre affects separation quality expectations and chord-vocabulary adequacy.
4. ~~LALAL.AI subscription?~~ Resolved: active — API adapter is the default separator, built in M1.

## Definition of done (phase 1)

On the target machine: open an existing REAPER project containing a song item → run `vgt: Analyze selected item` → pipeline completes (LALAL stems + local analysis) → `vgt: Apply results` augments the project per the tables above, non-destructively and idempotently; corrections in `corrections.yaml` survive re-runs; all analysis provenance is in `manifest.json`. Then phase 2 (practice controller: stem muting, loop/tempo management, take recording) builds on the same manifest and the same `[vgt]` track conventions.
