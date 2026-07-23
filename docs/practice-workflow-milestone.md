# First practice-workflow milestone (retired)

**Status: retired, not part of the current goal.** This design was ratified
twice (issues #89 and #105) and both times its implementation sub-issues
(#90–92, then #106–108) closed without landing code on `main`. It is kept
here only as a historical record. `docs/GOAL.md` does not authorize any
work from this document — do not create implementation issues from it
without an explicit new human decision to build the feature.

The rest of this document is the original design of record, preserved
as-is below.

---

This is the design of record for the first practice workflow.  It deliberately
turns the delivered analysis, stems, and reference MIDI into one useful session
without becoming a mixer, tempo editor, or recorder.  The implementation issues
listed at the end are the only work authorized by this design.

## Disposition of the #89 draft

This document ratifies the technical decision drafted for [#89](https://github.com/mrkkucharski/vgt/issues/89), rather than treating it as a rejected design.  The evidence is that #89 was closed with no merged pull request; its design commit `5688977` remains only on the former issue branch; and #90–#92 were all closed at the same time without merged pull requests.  Neither the issue record nor its comments records a rejection or a replacement decision.  The draft was therefore **abandoned operationally**, not rejected or superseded.

Issue #105 brings the design into the current branch after re-checking it against the delivered baseline and the permanent invariants.  The changes below are intentional ratification: the workflow and constraints are retained, the stale closed issue chain is replaced by the queued chain at the end of this document, and no practice-control implementation is implied by this decision issue.

## Decision

The first session is **loop the selected analysed section while practising
against the guitar-less `Backing` stem**.  It is available only when the
sidecar has both an effective section and a `backing` stem.  The musician
manually mutes their original reference-mix track in REAPER, invokes the new
practice action, picks a section, and presses Play.  The action makes the
vgt-owned stem mix suitable for that session, sets the chosen section as the
time selection and loop range, and enables repeat.  A separate Restore command
returns the saved loop and vgt-track mute state exactly as it was before the
session.

`Backing` is the existing guitar-less separation.  It is the smallest useful
bed because it avoids having to mute a user's original mix or build new routing.
If the original mix is still audible, the action warns rather than changes it.
The user can dismiss the warning after making their own choice; vgt must never
mute, solo, route, arm, rename, move, or otherwise modify a user-owned track.

No new audio analysis, separation, or transcription happens in a practice
session.  A missing backing stem or section is an actionable, read-only error:
run the already-delivered analysis/separation/apply workflow first.

## Capability decisions

| Capability | First-milestone decision | Reason and boundary |
| --- | --- | --- |
| Stem muting | **Commit.** Apply the fixed guitar-practice profile to vgt-owned *audio stem* tracks: Backing is unmuted; every other vgt-owned stem is muted. | It produces a usable accompaniment while changing only vgt objects.  Chords, Beats, Click, and reference-MIDI tracks keep their pre-session state; the action does not decide their visibility or audibility. |
| Stem soloing | **Defer.** Do not set any solo state. | REAPER solo changes the effective audibility of user tracks even if their flags are not rewritten.  A future mixer design can define an isolated monitoring/routing model if it is needed. |
| Section looping | **Commit.** Choose one effective `[vgt]` section; set its absolute-second bounds as both time selection and loop range, then enable repeat. | Effective human-synchronised sections are already the correction contract.  The choice is bounded and can be tested as data plus REAPER API calls. |
| Arbitrary time-selection looping | **Defer.** | It needs a durable distinction between a user's arbitrary selection and a vgt selection, plus a UI for choosing/quantising it.  The committed Restore safety model is intentionally exercised first with known section bounds. |
| Playback speed / playrate | **Defer as a control.** The action never sets global playrate or item rate. | A single playrate affects the whole project and needs pitch, metronome, and restoration policy.  Users may use REAPER's own controls; vgt neither persists nor restores their changes. |
| Tempo-map changes | **Defer.** The action never writes tempo markers or project tempo. | The permanent tempo-map invariant remains in force. |
| Recording preparation | **Defer.** No recording track, input, arm, monitor, record path, routing, FX, or transport-record control is created or changed. | Those are user/device-specific and need a separate non-destructive ownership model. |

## User interaction and command ownership

The UI is a new REAPER action, `reascript/vgt_practice.lua`, installed/run from
REAPER's Action List.  It has two explicit commands (separate action scripts or
a small command prompt with the same observable behaviour):

1. **Start guitar practice loop.** Read the adjacent sidecar and validate its
   managed identities, available backing stem, and effective sections.  If no
   session is active, present the section labels and bounds, save a snapshot,
   reconcile the stem mute profile, apply the chosen loop, and enable repeat.
   If a matching active session exists, reapply the desired profile and loop
   without taking a second snapshot.  If the previous snapshot's managed
   identities no longer match, restore only the still-identifiable state,
   clear the stale snapshot, and require an explicit fresh Start.
2. **Restore practice session.** Restore only the saved vgt-owned track mute
   flags and the exact prior loop/time-selection/repeat values, then clear the
   active snapshot.  It is safe and successful when there is no active session.

Initialization remains responsible only for its current task: creating and
reconciling the managed area and importing delivered artifacts.  Before it
removes/recreates managed tracks, it must safely clear/restore an active
practice snapshot (as defined below); it must not apply a practice profile.
The Python CLI remains analysis/status oriented.  It does not acquire a
`practice` mutation command and does not edit an RPP or a sidecar to start a
session.  A later read-only `vgt status` summary is optional, not a prerequisite
for the first interaction.

The action does not start, stop, seek, or record transport.  The user starts
and stops playback with normal REAPER controls.  This avoids a second category
of global state to restore.

## Sidecar state and ownership

The next schema revision adds a top-level `practice` object.  It has a small,
versioned **preference** portion and an ephemeral **active snapshot**:

```json
{
  "practice": {
    "version": 1,
    "preferences": {"profile": "guitar-backing"},
    "active_session": {
      "profile": "guitar-backing",
      "section": {"label": "Verse", "start_seconds": 30.0, "end_seconds": 46.0},
      "prior_loop": {"time_selection_start": 4.0, "time_selection_end": 9.0, "repeat_enabled": false},
      "prior_vgt_audio_mutes": {"{managed-guid}": false}
    }
  }
}
```

`preferences.profile` persists across sessions.  The selected section does
not: it is session state, so a later start always asks the user.  `active_session`
exists only between Start and Restore and is written before REAPER state is
changed.  It includes only GUIDs found in the sidecar's `managed_track_guids`
at snapshot time and only tracks the action independently verifies are
vgt-owned audio stems.  Its loop bounds are absolute project seconds, not
beats, measures, source samples, or item-relative offsets.

The sidecar remains vgt-owned metadata next to the project.  It must never
become a store for user track state, user routing, user tempo maps, user loop
choices after restoration, recordings, device/input names, or credentials.
The temporary prior-loop fields are the narrow exception: they exist solely to
return a global REAPER state that this action changed, and are deleted on
Restore.  Existing `managed_track_guids`, `managed_region_ids`, source
selection, analysis, and transcription records retain their current ownership
and migration behaviour.

## Reconciliation and restoration rules

Start and Restore are idempotent rather than toggle-by-accident operations.

- Start validates the backing artifact and section data before taking a
  snapshot.  With no snapshot, it records each eligible vgt audio stem's mute
  flag and the complete prior loop state exactly once.  With a matching
  snapshot, it only reasserts `Backing=false`, other eligible stems `true`,
  the saved chosen bounds, and repeat enabled.
- The profile is selected by GUID/sidecar ownership, never by a `[vgt]` name
  prefix alone.  A same-named user track is never eligible.  Missing or
  replaced managed tracks are skipped with a visible warning; no lookup falls
  back to a user track.
- Restore sets a mute flag only for a still-present GUID recorded in
  `prior_vgt_audio_mutes`, then restores the saved time selection and repeat
  flag as a unit.  It does not touch tracks introduced after Start.  It clears
  the snapshot only after the restoration calls have succeeded; a failure leaves
  enough state for a retry.
- Initialization, apply reconciliation, and any future vgt action that removes
  managed tracks must first perform the same best-effort restoration and clear
  the snapshot.  This prevents a removed/recreated track from making a stale
  snapshot appear valid.  It never resurrects or changes a missing track.
- A malformed sidecar snapshot, a changed project identity, or an unavailable
  required API is fail-closed: do not change REAPER state; tell the user to
  inspect/restore manually.  Schema upgrades add an inactive `practice` block
  and never infer an active session from current REAPER state.

All sidecar writes use the existing safe write path.  The action wraps its
successful REAPER mutations in one undo block named `vgt: start practice loop`
or `vgt: restore practice session`; sidecar persistence is still the authority
for a later explicit Restore.  Undo is convenience, not the recovery contract.

## Timing contract

Reference audio, separated stems, annotations, and imported reference MIDI
already use absolute project-time placement; vgt-owned audio and MIDI items are
time-based.  The practice action retains that contract:

- It derives section bounds from the effective section timeline plus the
  reference start, and writes loop selection in absolute seconds.
- It does not move, stretch, rate-change, or beat-attach any item, annotation,
  MIDI event, or region.
- It neither writes a tempo map nor invokes playrate changes.  Consequently a
  user tempo-map edit does not shift those time positions, and a user playrate
  change plays every currently audible time-based source at the same transport
  rate.  The loop still encloses the same source-time interval.

This is intentionally a time-aligned practice loop, not beat-relative looping.
If a future milestone offers beat-relative loops or vgt-controlled speed, it
must specify its own mapping and restoration behaviour before changing this
contract.

## Acceptance criteria

The implementation is accepted through offline, deterministic tests and static
contract checks.  No autonomous issue may require a live REAPER run, visual
inspection, or listening judgement.

- Schema migration yields an inactive `practice.version == 1` without changing
  existing analysis, ownership, or transcription fields.  Preferences persist;
  `active_session` round-trips and is removed after successful restore.
- Pure reconciliation tests cover first Start snapshot construction, repeat
  Start without snapshot replacement, missing/replaced managed GUIDs, malformed
  snapshots, and Restore that only targets recorded managed GUIDs.
- Lua-mocked tests prove that Start changes only verified vgt audio stem mute
  flags, uses no solo/routing/tempo/playrate/recording APIs, applies the chosen
  absolute-second loop selection and repeat state, and emits the expected undo
  labels.
- Lua-mocked Restore tests prove exact loop/time-selection/repeat restoration,
  restoration of pre-session mute flags, no mutation of unrecorded/new/user
  tracks, safe no-active-session behaviour, and retry-safe failure handling.
- Initialization/apply contract tests prove that an active session is restored
  or fail-closed before managed-track reconciliation, and ordinary initialization
  never starts a practice session.
- Existing `uv run pytest -q` remains green, including the permanent
  non-destructive, time-based media, tempo-map, and transcription regression
  contracts.

After automated checks, the user may perform this **human-owned checklist** in
a copy of a project: load both actions; manually mute the original mix; start a
section loop; listen for backing-only practice audio; change/stop playback;
run Restore; and confirm the prior loop and vgt stem mute states return.  Check
that no user track, routing, tempo marker, or recording configuration changed.
This is documentation for users, not an autonomous task dependency or an issue
acceptance gate.

## Follow-up implementation order

The linked GitHub issues are deliberately small and ordered.  They exclude
tablature/string-fret assignment, performance scoring, offline separation,
MIDI correction read-back, and DrumScript work.

| Issue | Work | Priority | Depends on |
| --- | --- | --- | --- |
| [#106](https://github.com/mrkkucharski/vgt/issues/106) | Practice sidecar schema and pure reconciliation/restore model | high | — |
| [#107](https://github.com/mrkkucharski/vgt/issues/107) | ReaScript Start/Restore practice-loop actions with mocked offline tests | high | #106 |
| [#108](https://github.com/mrkkucharski/vgt/issues/108) | Initialization integration, documentation, and regression contract for active-session handoff | medium | #107 |

They are linked as the GitHub sub-issue chain `#105 → #106 → #107 → #108`, so
the orchestrator cannot start a dependent implementation issue early.  Every
implementation issue is open with `status:queued`; #106 and #107 are
`priority:high` because they establish and exercise the safety boundary, while
#108 is `priority:medium` because it integrates that already-safe workflow.
