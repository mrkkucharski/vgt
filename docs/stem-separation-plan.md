# Stem Separation for vgt (M1)

Status: proposed · Date: 2026-07-20
Basis: `topics/music-ai/wiki/Music source separation.md` and
`topics/music-ai/wiki/AI-assisted instrument practice and performance assessment.md`
in the `llm-wiki` vault; the LALAL.AI REST API v1
(`https://www.lalal.ai/api/v1/docs/`, OpenAPI specification at
`https://www.lalal.ai/api/v1/openapi.json`); and vgt's existing sidecar /
stage-cache architecture.

This is the concrete implementation plan for milestone **M1** of
[phase1-song-prep-plan.md](phase1-song-prep-plan.md) (separation). Acceptance of
this plan pulls M1 into the active project scope; `docs/GOAL.md` is updated as
the immediate follow-up before implementation issues are started (see §6).

**Scope decision (2026-07-20): LALAL.AI is the sole separation backend.**
The Demucs offline fallback described in the original roadmap is **descoped**.
vgt's separation is therefore **cloud-only** for now; offline separation is a
deliberate non-goal (see §6). This trades away the wiki's "offline-capable"
principle for a smaller, simpler surface — accepted knowingly.

**Separation recipe (2026-07-20, fixed).** vgt reproduces the user's proven
manual LALAL workflow exactly — **no instrument probing, no configurable stem
set.** From the original mix it produces six artifacts via five paid split
operations:

1. **Vocal split — from the original.** `split(vocals)` → keep **both** the
   `vocals` stem *and* its back track as `instrumental` (mix − vocals). Both are
   loaded into REAPER.
2. **Instrument splits — from the original.** `split(bass)`, `split(drums)`,
   `split(guitar)` → keep only the isolated **`bass`**, **`drums`**, **`guitar`**
   stems; the back tracks ("the rest") are discarded.
3. **Guitarless practice bed — the one deliberate cascade.** Split guitar out of
   the **`instrumental`** from step 1 and keep the **back track** as `backing`
   (mix − vocals − guitar); the guitar stem from this split is discarded. This
   is a separation-of-a-separation, so its quality is knowingly lower — accepted
   because it is only the play-along bed, letting the student practice guitar
   over the band with neither the original guitar nor the vocals.

The reference stems (steps 1–2) are always isolated **from the original source**
to avoid accumulating artifacts. **Step 3 is the sole, intentional exception**,
and only for the convenience bed — never for a stem used as a reference.

Cost is accepted (five paid split operations per song); separation is rare
relative to practice. The hard requirement is **not redoing paid work**: known
remote work is durably checkpointed and resumed; an ambiguous submission fails
closed instead of being retried at the risk of a second charge.

## 1. What the sources tell us

**From the music-ai wiki:**

- LALAL.AI is the cloud default; it exposes dedicated **acoustic-** and
  **electric-guitar** stems — and *guitar is the one stem that must be good*
  for a practice workflow. This is the decisive reason to build on LALAL.
- Demucs (the offline 4-stem alternative) puts guitar into an undifferentiated
  "other" stem and only isolates guitar via an experimental 6-stem model the
  wiki warns against — so it never satisfied "the one stem that must be good"
  anyway. Descoping it removes an offline path, not guitar quality.
- UVR / RoFormer-family models remain a *possible future* alternative backend,
  not part of this plan.
- Design rule that still applies: evaluate every stem for **interference and
  damage** (residual vocals, warble, lost transients) — the decisive test is
  whether the stem works for practice, not a numeric separation metric.
- Resilience caveat: LALAL is subscription-gated. With no offline fallback,
  resilience now means **failing clearly and cheaply** (credits are spent once
  per song via caching; a lapsed/exhausted plan produces a clear error, never a
  silent or corrupt result), not degrading to a local model.

**From LALAL API v1:**

- API v0 (`/api/upload/`, `/api/split/`, `Authorization: license …`) is
  deprecated and is not used by vgt.
- Auth: `X-License-Key: <key>` on every v1 request. The key never appears in a
  URL, sidecar, exception, progress message, or recorded test fixture.
- Flow: `POST /api/v1/upload/` (→ source id + duration + expiry) →
  `POST /api/v1/split/stem_separator/` (→ task id) → poll
  `POST /api/v1/check/` with task ids (→ progress %, then typed `stem` and
  `back` tracks with download URLs).
- A stem-separator operation isolates **one stem** and returns that stem plus a
  back track. Supported values include `electric_guitar`, `acoustic_guitar`,
  `vocals`, `drum`, `bass`, `piano`, `synthesizer`, `strings`, and `wind`.
- Splitter values currently include Andromeda, Perseus, Orion, Phoenix, Lyra,
  and Lynx; `auto` selects a current model. Relevant presets include
  `dereverb_enabled`, `encoder_format`, and `extraction_level`
  (`deep_extraction` | `clear_cut` — the v1 quality lever; the old v0
  `enhanced_processing_enabled` preset no longer exists). vgt explicitly
  requests `encoder_format: "wav"`.
- A `/api/v1/split/multistem/` endpoint can return several stems from one
  request. vgt does **not** use it: billing is still per stem-minute (no credit
  saving), and one task producing many outputs would break the per-operation
  idempotency/resume model below, where each paid stem is its own durably
  checkpointed task. Steps 1–2 therefore stay as separate `stem_separator`
  operations by design, not oversight.
- `POST /api/v1/limits/minutes_left/` reports remaining processing minutes.
  Billing remains proportional to source duration for each requested stem.
- `/api/v1/check/` is currently limited to 30 requests/minute; polling uses
  bounded backoff and honors `429` responses.
- Split requests accept a UUID idempotency key. It is a duplicate-charge guard,
  not a substitute for durably recording the returned task id.
- Source uploads and task checks expire after 24 h. vgt may call
  `/api/v1/delete/` after every required output is safely local; otherwise it
  relies on expiry.

### The critical design consequences

Because the selected v1 endpoint isolates **one stem per paid operation**
(returning that stem and a back track), the recipe maps onto LALAL as:

- **Two uploads.** Upload the original once (file id `O`); the four
  original-source splits (vocals, bass, drums, guitar) all run against `O`. Then
  upload the `instrumental` artifact once (file id `I`) for the single step-3
  guitar split.
- **Five paid operations, ≈ 5× song duration in credits.** Cost estimation
  counts outstanding operations, not output files: the vocal operation produces
  two retained artifacts but is billed once. `POST /api/v1/limits/minutes_left/`
  is checked before starting any outstanding task. Uploaded duration is the
  authoritative estimate once available.
- **Which side of each split is kept** is what distinguishes the artifacts:
  step 1 keeps *both* sides (vocals stem + instrumental back track), step 2 keeps
  only the *stem* side, step 3 keeps only the *back-track* side. The orchestration
  layer records both operation ownership and per-artifact side.
- **Durable remote identity.** Each operation records its idempotency key,
  source id/expiry, task id, requested presets, effective presets, and state as
  soon as each value is known. A restart resumes polling/downloading rather than
  submitting a replacement task.
- **24 h expiry.** `O` or `I` is re-uploaded only when its recorded id has
  expired. Expiry of a completed remote task does not matter after validated
  local outputs have been committed.

## 2. How it fits vgt's architecture

Reuse the existing sidecar concept, but do not apply detector-cache semantics
unchanged to paid binary operations:

- **Content identity for paid work.** Separation uses a full SHA-256 of audio
  bytes, computed once and stored in the sidecar. The existing cheap
  path/size/mtime detector hash is not sufficient: moving the project must not
  trigger a new charge, and different bytes must never reuse old stems.
- **Operation cache, artifact index.** The `stems` stage owns five operation
  records and a six-artifact index. Cache validity requires a matching source
  content hash + spec hash and validated local output. `human_verified` records
  a listening judgment only; unlike detector stages, it never overrides source,
  settings, or file-validity checks.
- **Fixed operation identities and dependencies.** The records are
  `vocals-original`, `bass-original`, `drums-original`, `guitar-original`, and
  `guitar-instrumental`. The last depends on the instrumental output of
  `vocals-original`; cache invalidation follows this DAG rather than invalidating
  unrelated work.
- **Crash-safe checkpoints.** Sidecar updates use temp-file + atomic replace and
  occur after every remote state transition and every committed output. A
  completed download is first streamed to `.part`, checked for HTTP length and
  readable WAV metadata/duration, then atomically renamed. A partial file is
  never treated as cached.
- **Concurrency protocol (no shared-JSON races).** A multi-minute separation
  and a user-triggered `vgt_initialize.lua` must not mutate the sidecar
  concurrently. Two rules make this safe rather than merge-on-hope:
  - *In-progress marker.* Python writes a `stems.in_progress` lease (pid +
    UTC start + heartbeat) into the sidecar before the first paid task and
    clears it when the run ends. `vgt_initialize.lua` refuses to apply while a
    live lease is present (offering to retry later); a stale lease past a
    timeout is ignored so a crashed run never wedges the project.
  - *Field ownership.* Python owns only the `analysis.stems` subtree; the
    ReaScript owns tracks/regions/`managed_*`/reference config. Each writer
    reads-modifies-writes **only its own subtree** under the lease, via the
    temp-file + atomic-replace already specified — so neither clobbers the
    other's fields even in the residual race window.
- A changed reference source or guitar type stops submission of further tasks
  and lets the next run recalculate the DAG.
- **Recipe orchestration outside the backend.** A vgt separation orchestrator
  owns the fixed five-operation DAG, cache decisions, filenames, and sidecar
  checkpoints. A thin `Separator` interface executes one split operation and
  accepts opaque resume state plus a checkpoint callback. This isolates
  HTTP/LALAL details while allowing a fake backend to exercise the same retry
  and resume logic. Only `LalalSeparator` is implemented now.
- **Light dependency**: LALAL needs only an HTTP client (`httpx` or `requests`)
  — a small default dependency, **no torch and no optional extra** (a direct
  benefit of dropping Demucs).
- **Sidecar `.vgt`** is the manifest; add a `stems` block (not the plan's
  separate `manifest.json`).
- **ReaScript `vgt_initialize.lua`** already imports media as `[vgt]` tracks
  (the mirror); extend it to import stem WAVs the same way.

### Artifact layout

All vgt artifacts live **in the user's REAPER project folder**, never in this
repo. Regenerable outputs move under a stable per-project namespace inside a
**`vgt/` subfolder next to the RPP**, while the canonical `.vgt` sidecar stays
adjacent to the project. On first upgrade, vgt creates and persists an
`artifact_namespace` such as `<project-stem>-<short-id>`; it does not change if
the RPP is renamed or a second RPP later appears in the same folder.

```
<song folder>/                     # the user's project folder, outside this repo
  <project>.RPP                    # REAPER project        (source of truth)
  <project>.vgt                    # sidecar / canonical vgt state — stays put
  vgt/                             # generated, regenerable artifacts
    <artifact-namespace>/          # stable id recorded in <project>.vgt
      stems/
        vocals.wav  instrumental.wav  bass.wav
        drums.wav   guitar.wav        backing-no-guitar.wav
      tempo-click.wav
      chords.txt
      sections.txt
```

Why the sidecar stays put: it is the one canonical vgt file, its `.RPP → .vgt`
adjacency is an established phase-0 convention, and a folder named `<project>.vgt`
would collide with the sidecar file of the same name.

Benefits beyond tidiness: stems living **under the project directory** allow
REAPER to save project-relative media references, so moving or backing up the
whole song folder does not break items; and "remove this project's generated
files" becomes deleting one sidecar-recorded namespace instead of globbing
`<project>.vgt-*` siblings. A live REAPER test must verify the saved RPP actually
contains relative paths; supplying a local file to the API alone is not treated
as proof of serialization behavior.

**Nothing here is chosen for git's sake.** A user's project folder is outside
this repo, so `.gitignore` is irrelevant to real usage — `vgt/` is just output
beside their `.RPP`. Git enters only through this repo's checked-in **test
fixture** (`test/Reaper Project/`): dev/CI runs generate artifacts into it, so a
single ignore rule **scoped to the fixture path** (`test/**/vgt/`) keeps the repo
clean. (If a user chooses to version their own project, adding their own
`.gitignore` is their call, not vgt's.)

## 3. Proposed work — sub-issues

### A — Operation model + `Separator` seam + fake backend (priority:high, L)

- New `src/vgt/separation.py` owns the fixed five-operation DAG, cache checks,
  content hashes, output validation/naming, and sidecar checkpoints.
- Define a one-operation `SplitSpec`: requested stem, source role, retained
  side(s), splitter, `dereverb_enabled`, `extraction_level`
  (`deep_extraction` | `clear_cut`), and fixed `encoder_format="wav"`.
  `guitar_type` (`electric` / `acoustic`) selects the requested stem for the two
  guitar operations.
- Define a thin `Separator` protocol conceptually equivalent to
  `split(source, out_dir, spec, *, resume_state, checkpoint) -> SplitResult`.
  `resume_state` is backend-opaque; `checkpoint` lets a backend durably publish
  remote identity/state immediately. The orchestrator, not the backend, owns
  the recipe and cache policy.
- **`guitar_type` is declared, never detected.** On first REAPER initialize, a
  menu asks Electric / Acoustic and stores the answer in sidecar config; the
  `vgt/guitar_type` ExtState can supply it for automation. The CLI's
  `--guitar` overrides and persists it. With neither flag nor persisted value,
  an interactive CLI prompts; a non-interactive CLI falls back to `electric`
  and reports that choice. A persisted value prevents duplicate prompts.
  Changing it invalidates only the original-guitar and backing operations (two
  charges), not vocals, instrumental, bass, or drums.
- Add a stable `artifact_namespace` and an `analysis.stems` block with
  `backend`, `api_version`, `recipe_version`, `guitar_type`, `operations`, `artifacts`,
  `human_verified`, and `verified_at`. Each operation records:
  `{source, source_sha256, spec_hash, requested_presets, effective_presets,
  backend_state, status, outputs, completed_at, error}`. LALAL backend state
  includes source id/expiry, idempotency key, and task id. Each artifact records
  `{file, operation, side, sha256, size_bytes, duration_seconds, separated_at}`.
- Schema upgrade preserves all prior fields. `human_verified` is quality
  metadata only and never makes stale/missing outputs current.
- Ship a configurable `FakeSeparator` that writes valid short WAVs and can fail
  before or after every checkpoint/download. All recipe, cache, partial-resume,
  and sidecar tests run against it with **no network**.

### B — LALAL API v1 backend (priority:high, L)

- Implement upload, split, check, cancel/delete, limits, and download using the
  official v1 endpoints and response schemas. The `X-License-Key` value comes
  from **`LALAL_LICENSE_KEY`** and is never persisted or logged.
- Execute the fixed recipe: upload original → vocals [keep stem + instrumental]
  → bass/drums/guitar from original [keep stems] → upload instrumental → guitar
  [keep back as backing]. The final guitar operation is the only cascade.
- Before any outstanding paid task, call `/api/v1/limits/minutes_left/` and
  compare it with the sum of outstanding operation durations. Count the vocal
  stem/back pair once. A run that cannot cover the complete outstanding recipe
  starts no new paid tasks.
- Generate and persist a UUID idempotency key before submission; persist the
  task id immediately after a successful response. On restart, resume a known
  task. An ambiguous submission whose task identity cannot safely be recovered
  fails closed with an actionable message rather than risking a second charge.
- Poll below 30 requests/minute with bounded backoff; handle `429`, task errors,
  cancellation, upload/task expiry, and expired download URLs. Re-upload only
  expired sources and never replace a completed operation merely because its
  remote record expired.
- Stream downloads to `.part`, validate response length plus readable WAV
  metadata and plausible duration, then atomically rename and checkpoint. Store
  output SHA-256/size/duration. A missing or corrupt retained output invalidates
  its owning operation; a paired output that remains valid is preserved while
  the paid operation is deliberately recovered.
- Record requested and effective presets, including the actual model selected
  by `auto`. A retry needed to reconstruct one output pins the recorded model
  when the API still supports it, avoiding mismatched pairs.
- Mock v1 HTTP fixtures in CI; no live API calls. Keep one explicitly manual,
  opt-in smoke-test procedure for an account owner before release.

### C — CLI wiring + partial-success semantics (priority:medium, M)

- Fold separation into `vgt analyze` as the last stage, with progress on stderr.
  Refactor persistence so each successful local stage and each separation
  checkpoint is atomically written immediately. If LALAL is unavailable, local
  tempo/key/sections/chords results remain saved; the command reports the stems
  error and exits nonzero after describing the partial success.
- The existing `--force` refreshes only non-paid local analysis. It **never
  re-bills cached stems**. Deliberately repeating paid work requires
  `--force-stems`, an exact operation/cost preview, and interactive confirmation;
  non-interactive automation must also pass an explicit acknowledgment flag.
- The recipe and artifact set stay fixed. `--guitar electric|acoustic` is the
  only song-level choice and follows A's persistence rules. Before spending,
  print remaining minutes, estimated operation cost, and cached/outstanding
  operations (not merely artifact count).
- `vgt status` reports the five operations and six artifacts, requested/effective
  splitter and guitar type, remote/in-progress/error state, local file validity,
  and quality-verification status without exposing credentials.

### D — ReaScript import of stems (priority:medium, M)

- Extend `vgt_initialize.lua`: after the mirror, read `analysis.stems.artifacts`
  from the sidecar and create six `[vgt]` stem tracks — `[vgt] Vocals`,
  `[vgt] Instrumental`, `[vgt] Bass`, `[vgt] Drums`, `[vgt] Guitar`, and
  `[vgt] Backing (no guitar)` — **time-based** positioning, offset-shifted,
  GUIDs tracked in `managed_track_guids`, idempotent on re-apply — the same
  discipline as the existing `[vgt]` tracks. `[vgt] Backing (no guitar)` is the
  guitarless play-along bed from step 3. Resolve files only through the
  sidecar-recorded artifact namespace; missing/invalid artifacts are skipped
  with a clear warning. Insert WAVs as time-based media and verify in a saved
  real RPP that REAPER serializes project-relative paths so the song folder stays
  portable. Audible now; a later practice phase automates practice muting.

### E — Consolidate artifacts under `vgt/` (priority:medium, S)

- Move the existing regenerable artifacts into the stable namespace under
  `vgt/`: `tempo-click.wav`, `chords.txt`, `sections.txt`. Touches
  `tempo.py` (`click_artifact_path`), `chords.py` (`chord_sheet_path`),
  `sections.py` (`section_timeline_path`), `status.py` (artifact-path reporting),
  and the repo's fixture-scoped `.gitignore` (replace the old `*.vgt-*.wav` /
  `*.vgt-sections.txt` patterns with `test/**/vgt/`). The `.vgt` sidecar itself
  does **not** move.
- The schema migration creates `artifact_namespace` once and updates new output
  paths. Existing legacy artifacts may be regenerated or copied only when their
  exact prior vgt-owned path is known. Unknown files are never swept by a glob;
  harmless legacy orphans remain unless an explicit cleanup action identifies
  them. Land this before B and D so both learn only the final layout.

### F — Evaluation pass (user-owned, not an agent task)

- On the golden songs, listen for interference/damage in the LALAL guitar and
  backing stems; note electric-vs-acoustic, effective splitter, and
  `extraction_level` (`deep_extraction` vs `clear_cut`) quality in a findings
  doc. Feeds the deferred M7 evaluation.
- **This is a human-listening task the user performs directly** — it needs human
  ears and owned audio, so it is not tracked as an autonomous-agent issue. Do not
  create or re-create an issue for it.

## 4. Sequencing & risks

- Order: **A → E → B → C → D**, then **F** as a user-run evaluation once real
  output exists. A establishes the operation/cache contract with no external
  dependency; E fixes paths before outputs exist; B is the only network-touching
  piece; C supplies durable stage orchestration; D consumes the final
  sidecar/path contract; F (user-owned, not an agent issue) evaluates real output.
- **Secrets**: `LALAL_LICENSE_KEY` is environment-only — documented in the
  README, never in the repo or the sidecar.
- **Cost is deliberately higher, not accidental**: five operations per song is the
  fixed recipe because prep is rare relative to practice and quality wins. The
  guard against waste is not fewer splits but **not repeating** them —
  operation caching, durable remote identity, atomic output commit, and a
  pre-flight cached/outstanding cost breakdown. Ordinary `--force` never spends.
- **Durability scope is deliberately bounded.** Two invariants are
  **must-have** and drive the mandatory machinery: (1) **never double-charge**
  (idempotency key + durably recorded task id + fail-closed on ambiguity) and
  (2) **never import a partial/corrupt stem as valid** (`.part` streaming, WAV
  validation, atomic rename, recorded SHA-256). Everything else — the full
  crash-injection matrix, pinning the `auto`-selected model on a reconstruction
  retry, heartbeat leasing — is **hardening, not a gate**: valuable, but a
  reviewer should scope it against a rarely-run personal tool and may defer
  pieces without weakening the two must-haves. Size L on A/B assumes the
  must-haves plus a *subset* of the hardening, not all of it.
- **Cascade only where declared**: the reference stems (vocals, instrumental,
  bass, drums, guitar) are split from the **original** to avoid accumulating
  artifacts; the *only* permitted cascade is the step-3 `backing` (guitar
  removed from the instrumental), whose lower quality is accepted because it is a
  convenience bed, not a reference stem. Both the from-original rule and its
  single exception are correctness invariants worth explicit tests.
- **Cloud is now the *only* path** (the central risk of descoping Demucs):
  a LALAL API change, credit exhaustion, or a lapsed subscription **blocks
  separation entirely**. Mitigation is graceful failure + caching, not fallback
  — a clear error, cached stems preserved, and independently checkpointed local
  stages (tempo/key/chords/sections) still complete.
- **Ambiguous remote failure is fail-closed**: if vgt cannot prove whether a
  submission was accepted and cannot recover a task id, it does not retry the
  paid operation automatically. This can require manual account inspection, but
  it preserves the stronger no-double-charge invariant.
- **No offline capability** is an accepted, documented limitation of this plan
  (a deliberate deviation from the wiki's offline-capable principle); revisit
  with UVR/RoFormer if offline separation is ever needed (§6).
- **Writing into a live project** (ReaScript import): mitigated by the `[vgt]`
  prefix, additive-only writes, GUID tracking, and idempotent re-apply.

## 5. Acceptance criteria

- API tests use v1 request/response fixtures and assert `X-License-Key` is
  redacted from exceptions, progress, sidecars, and snapshots.
- Injected termination before/after upload response, split submission,
  task-id checkpoint, polling completion, each download, validation, rename,
  and sidecar write resumes without silently submitting a duplicate paid task.
- The vocal operation is charged/counts once while producing two retained
  artifacts; changing guitar type invalidates exactly two paid operations.
- Plain `vgt analyze --force` submits zero cached split operations.
  `--force-stems` cannot spend without cost disclosure and explicit consent.
- Missing key, exhausted credits, timeout, `429`, task error, expired source,
  corrupt/truncated download, and disk-write failure all preserve prior valid
  artifacts and independently completed local analysis.
- Moving the whole song folder keeps the source content hash and cache current.
  Changing source bytes invalidates every dependent operation; changing an
  operation preset invalidates only its downstream dependency closure.
- Every committed artifact is a readable WAV with recorded SHA-256, size, and
  plausible duration. `.part` files are never reported/imported as complete.
- `vgt status` accurately distinguishes cached, outstanding, in-progress,
  failed, missing, corrupt, and human-verified state without making network
  calls or mutating the project.
- A real saved-project test proves six stem tracks are offset-shifted,
  time-based, project-relative, `[vgt]`-owned, and idempotent on re-apply; user
  tracks and regions remain untouched.
- A manual opt-in LALAL smoke test on one short owned audio file confirms the
  v1 contract and cost estimate before release. CI never spends credits.

## 6. Scope disposition

- **Phase framing is resolved.** Acceptance of this plan pulls M1 into the
  active project scope. Update `docs/GOAL.md` before creating implementation
  issues so the goal, issues, and orchestrator agree.
- **Offline separation remains explicitly deferred.** If an offline path is
  later required, the one-operation `Separator` seam (§2) is where a
  UVR/RoFormer or Demucs backend attaches — a future phase, not this one.
