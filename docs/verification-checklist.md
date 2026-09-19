# Verification checklist and regression contract

Maintainer reference, moved out of the user manual. The user manual keeps a
short list of user-facing guarantees; this file holds the full contract and
the live REAPER checks.

## Human-owned REAPER verification

Automated tests cover static artifacts only (readable MIDI, well-formed JSON,
REAPER placement through a fake-REAPER harness). They never open REAPER or
judge whether a transcription sounds right. These checks are for the user in a
real REAPER session. They are never an autonomous-agent acceptance gate and
never a reason to block closing an issue.

Transcription:

- Listen to the drums reference MIDI against the `Drums` stem with a drum kit
  loaded. It should span the stem and stay aligned throughout; a whole-take
  drift (the DrumScript half-tempo bug, #193) should not recur. Per-note
  misses, extras, misclassifications and tens-of-milliseconds looseness are
  normal detection error, not a timeline bug.
- Check that each reference MIDI track sits directly below its stem and stays
  aligned after apply/sync.
- Compare guitar detail and clean candidates against the same stem. Check that
  the spectral gate kept intentional octave shapes; use
  `scripts/guitar_transcription_probe.py` for ghost share, polyphony,
  fragmentation and chord-tone metrics.

Ownership and layout:

- Create a `[work]` copy from a generated candidate, edit it, run
  analyze/apply, and confirm it is intact. Discard a generated variant and
  confirm only its track and artifacts disappear.
- On a disposable copy with out-of-order or hand-made unmarked
  `[clean]`/`[work]` folders, run initialize: the three containers end up in
  order, are adopted rather than duplicated, and their contents are untouched.
- Promote a `[work]` track: it lands in `[clean]`, keeps items and FX, is
  renamed, and survives a following initialize.
- Confirm an empty `[clean]` does not swallow the tracks below it.

Duplicate-`[vgt]`-folder protection (#174):

- Run initialize twice on a disposable copy of a real project: exactly one
  `[vgt]` folder both times.
- Add a transcription variant (or rerun `vgt analyze` with different
  settings), initialize again, and confirm the root folder is reconciled in
  place.
- Save, close and reopen REAPER, then repeat both checks to exercise the
  persisted manifest and per-track marks.

## Permanent regression contract

- **Reconciliation vs. working-copy actions:** initialize/apply changes only
  `[vgt]`-managed objects. It may create, rename, recolour and reposition the
  `[clean]`/`[work]` container tracks and move their blocks as a unit, but
  never touches their contents. The create action affects only new copies;
  promotion alone moves and renames selected tracks that still carry the
  working-copy mark and a `[work]` name. A request that would rewrite the
  folder depth of an unselected container child is rejected unchanged.
- Initialize keeps the bottom layout: loose root tracks, `[clean]`, `[work]`,
  `[vgt]`, then `[vgt] MT3` when present.
- Re-running initialize or analyze creates no duplicate managed tracks,
  regions or stems.
- Ownership survives an interrupted apply: it is recorded both in the sidecar
  and in the project (per-track marks, project-scoped state for regions) and
  reconciled from the union of both.
- Tracks record a stable role (`managed-root`, `beats`, `key`, `chords`,
  `stem:<name>`, `variant:<target>:<id>`), and the project stores a
  managed-root manifest. If apply cannot authenticate a `[vgt]` folder, or
  finds several candidate roots, it stops before changing anything and reports
  the paths and ownership counts. A `[vgt]` name never grants deletion
  permission. To recover, back up, keep the authenticated root, and rename
  each unauthenticated folder to a non-container, non-`[vgt]` name (e.g.
  `[archive] duplicate`); it is then preserved as user-owned.
  - Exception: if an apply was interrupted after marking the whole rebuilt
    tree but before its final manifest write, the live root carries its own
    ownership evidence, so the next apply resyncs the manifest and proceeds.
- Project mutation uses REAPER's API, never RPP text editing.
- Heavy analysis runs in the CLI, not inside REAPER.
- vgt-owned audio is time-based and does not stretch with tempo-map changes.
- Synced section corrections survive analysis and apply. Chord and key
  corrections survive only when promoted into a user-owned `[clean]` copy.
- Plain `--force` makes no LALAL charges; paid work is cached, checkpointed,
  and explicitly confirmed when forced or optional.
- Transcription runs locally after separation, never triggers paid
  separation, and caches each backend's raw detection separately from each
  retained variant's derived artifacts.
- Generated variants (peers, ordered only for presentation) and user-owned
  `[work]` copies have distinct ownership, tracked through provenance rather
  than colour.
- Chord analysis stays audio-based (original mix plus available
  instrumental/guitar/backing stems). Generated or clean MIDI is never fed
  back into it.
- DrumScript backs `drums`, pYIN `bass`, Essentia Klapuri `guitar` by
  default, Basic Pitch every other target. A backend failure records a
  per-target error and never falls back to another backend.
- Reference MIDI tracks are unmuted, time-based and paired with their source
  stem; edits must be copied to a user-owned track first.
- `vgt status` is read-only and never reveals the license key.
