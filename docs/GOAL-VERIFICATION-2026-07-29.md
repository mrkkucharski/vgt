# Offline goal verification — 2026-07-29

This is the final local certification of the goal as it stood at source commit
`1e20622d3d3e114212d58cffd792fd0c5c3ac709` (`Merge pull request #242 from
mrkkucharski/issue-239-reconcile-container-invariants-with-expl`). This record deliberately lives in the repository so the evidence
does not depend on an ephemeral runner, GitHub Actions, or an issue comment.

## Result

The focused acceptance contract and complete suite both passed under the
project's supported CPython 3.11.15. The credential variable was explicitly
removed. `sandbox-exec` denied every network operation and `uv --offline`
forbade dependency resolution from the network. The acceptance contract uses
deterministic fakes for the LALAL and transcription seams, so it did not use a
credential, network, model download, or live REAPER. Collection was run before
each execution: all 17 focused tests and all 581 full-suite tests executed and
passed, with no skips.

```console
$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv run --offline --python /Users/marekkucharski/projects/proj-mgr/orchestrator/.venv/bin/python3.11 \
    pytest --collect-only -q tests/test_goal_contract.py
17 tests collected in 0.11s

$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv run --offline --python /Users/marekkucharski/projects/proj-mgr/orchestrator/.venv/bin/python3.11 \
    pytest -q tests/test_goal_contract.py
17 passed, 4 warnings in 27.03s

$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv run --offline --python /Users/marekkucharski/projects/proj-mgr/orchestrator/.venv/bin/python3.11 \
    pytest --collect-only -q
581 tests collected in 0.47s

$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv run --offline --python /Users/marekkucharski/projects/proj-mgr/orchestrator/.venv/bin/python3.11 \
    pytest -q
581 passed, 14 warnings in 51.32s
```

The warnings were deprecations/fallback notices from audioread/librosa; no test
failed or was skipped. `/private/tmp` was writable and was supplied explicitly
because prior certification collection failed without a writable temporary
directory.

## Environment

| Item | Observed value |
| --- | --- |
| OS / architecture | macOS 26.5.2 (25F84), arm64 |
| Python | CPython 3.11.15 |
| uv | 0.11.7 |
| pytest | 9.1.1 |
| Lua test interpreter | Lua 5.5.0 |
| librosa / NumPy | 0.11.0 / 1.26.4 |

## Built-package evidence

The distribution check used the same credential-free, network-denied sandbox;
it did not import from the checkout after installation.

```console
$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv build --offline --wheel --out-dir /private/tmp/vgt-issue-243.lkafAk/dist
Successfully built .../vgt-0.1.0-py3-none-any.whl

$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv venv --offline --python /Users/marekkucharski/projects/proj-mgr/orchestrator/.venv/bin/python3.11 \
    /private/tmp/vgt-issue-243.lkafAk/venv
$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY UV_OFFLINE=1 TMPDIR=/private/tmp \
    uv pip install --offline --python /private/tmp/vgt-issue-243.lkafAk/venv/bin/python \
    /private/tmp/vgt-issue-243.lkafAk/dist/vgt-0.1.0-py3-none-any.whl
Installed 31 packages, including vgt==0.1.0 from the wheel

$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY \
    /private/tmp/vgt-issue-243.lkafAk/venv/bin/vgt --help
# exited 0; listed inspect, apply, sync, analyze, status, install-reascripts,
# and transcription

$ sandbox-exec -p '(version 1) (deny network*) (allow default)' \
    /usr/bin/env -u LALAL_LICENSE_KEY \
    /private/tmp/vgt-issue-243.lkafAk/venv/bin/vgt install-reascripts \
    --destination /private/tmp/vgt-issue-243.lkafAk/Scripts/vgt
Installed: .../vgt_initialize.lua
Installed: .../vgt_sync.lua
Installed: .../vgt_sync_tempo_map.lua
Installed: .../vgt_working_copy.lua
```

From `/private/tmp`, the installed package resolved to
`/private/tmp/vgt-issue-243.lkafAk/venv/lib/python3.11/site-packages/vgt/__init__.py`;
all four bundled ReaScripts therefore came from the wheel rather than the source
checkout.

## Goal coverage audit

The focused acceptance contract is the end-to-end proof; the named full-suite
tests below provide the targeted regression evidence for every delivered
capability and permanent invariant in `docs/GOAL.md`.

| Goal claim | Executable coverage |
| --- | --- |
| Phase 0: locate a real RPP, choose and persist one file-backed reference, retain a sidecar, and create the live managed area | `test_goal_contract_is_offline_non_destructive_and_idempotent`; `test_second_apply_reuses_the_persisted_reference_without_prompting_in_a_multi_track_project`; `test_apply_uses_reaper_api_and_never_edits_rpp_text` |
| Phase 1: tempo/grid, key, sections, beat-aligned chords, and visible REAPER results | `test_goal_contract_is_offline_non_destructive_and_idempotent`; `test_phase1_apply_reads_analysis_and_uses_only_reaper_api`; `test_chords_fall_back_to_freshly_detected_beats_when_tempo_correction_omits_them` |
| Live mutation stays in the REAPER API while analysis stays in the Python CLI | `test_apply_uses_reaper_api_and_never_edits_rpp_text`; `test_sync_only_reads_reaper_state_and_never_mutates_the_rpp`; `test_analyze_writes_v2_sidecar_with_skeleton_and_provenance` |
| Corrections persist while detected baselines remain available; ordinary sync does not synchronize tempo markers | `test_goal_contract_is_offline_non_destructive_and_idempotent`; `test_manual_correction_survives_rerun`; `test_key_and_chord_corrections_survive_rerun`; `test_sync_writes_corrected_chords_and_sections_in_one_invocation` |
| Confirmation-gated tempo-map correction is reference-relative and safe | `test_goal_contract_tempo_map_sync_drives_variable_grid_without_touching_user_map`; `test_tempo_map_sync_is_a_separate_confirmation_gated_read_only_action`; `test_apply_never_overwrites_a_user_tempo_edit_made_after_an_interrupted_commit` |
| LALAL standard and optional stems are recipe-driven, cached, checkpointed, and cost-gated | `test_goal_contract_exercises_cli_paid_stem_cost_controls_and_resume`; `test_full_run_completes_all_five_operations_and_six_artifacts`; `test_resuming_after_a_crash_right_after_checkpoint_never_double_charges`; `test_opt_in_strings_and_piano_are_priced_persisted_and_cached_on_retry` |
| Transcription variants are immutable-ID peers with labels and stable presentation only; Basic Pitch detection is shared where appropriate, and ADTOF may coexist with DrumScript | `test_goal_contract_reconciles_two_guitar_variants_without_touching_working_copies`; `test_detail_and_clean_share_one_detection_hash_and_one_backend_invocation`; `test_status_shows_ordered_variants_without_selection`; `test_variant_add_drums_adtof_coexists_with_drumscript_and_receives_the_project_grid` |
| Every supported transcription target is exercised end-to-end: Basic Pitch for guitar, bass, vocals, piano, strings, instrumental, backing, and original mix; DrumScript by default for drums, with opt-in ADTOF retained beside it | `test_goal_contract_reconciles_independent_guitar_bass_and_drum_targets`; `test_goal_contract_exercises_every_remaining_basic_pitch_target_end_to_end`; `test_goal_contract_retains_and_discards_adtof_beside_drumscript_offline`; `test_goal_contract_keeps_drumscript_variants_on_the_project_timeline` |
| Non-destructive and idempotent behavior, including preservation of user tracks/items/regions and user-owned `[work]` copies | `test_goal_contract_is_offline_non_destructive_and_idempotent`; `test_goal_contract_reconciles_the_7rivers_incident_fixture_into_a_single_root`; `test_variant_reconciliation_removes_generated_tracks_but_preserves_a_working_copy` |
| `[clean]`, `[work]`, `[vgt]` containers are canonical, ordered, and their contents are not changed or reordered | `test_goal_contract_adopts_interleaved_handmade_containers_and_moves_their_blocks`; `test_fresh_project_creates_both_containers_flattened_and_ordered`; `test_containers_starting_interleaved_or_below_vgt_end_up_correctly_ordered`; `test_adopts_unmarked_named_and_bare_legacy_containers_leaving_contents_and_colour_alone` |
| The working-copy action creates only into an empty `[work]` container and refuses a populated container unchanged; promotion moves only eligible selected marked tracks and refuses if folder repair would alter an existing container child | `test_create_refuses_a_populated_work_container_without_touching_its_child`; `test_create_reuses_an_empty_initialize_container`; `test_promote_moves_the_existing_track_and_reclaims_it`; `test_promote_into_empty_clean_preserves_selection_order_and_flattens_work`; `test_promote_refuses_renamed_or_unmarked_work_tracks_without_changes`; `test_promote_refuses_when_folder_repair_would_change_unselected_content`; `test_goal_contract_is_offline_non_destructive_and_idempotent` |
| Generated data is not chord ground truth; cleanup/discard and cache GC stay scoped | `test_chord_source_set_uses_only_the_measured_fusion_artifacts`; `test_discard_uses_recorded_artifact_paths_and_never_escapes_namespace`; `test_gc_never_deletes_a_path_recorded_outside_its_own_cache_namespace` |
| Credentials are environment-only and are never persisted; explicit consent and recovery prevent unintended paid repeat work | `test_goal_contract_exercises_cli_paid_stem_cost_controls_and_resume`; `test_cli_force_stems_requires_explicit_noninteractive_acknowledgment`; `test_resuming_after_a_crash_right_after_checkpoint_never_double_charges` |

## Deliberately outside this acceptance

Guided practice is not part of the current goal. Subjective listening and
live-REAPER verification remain human-owned checks. GitHub Actions is not
used: hosted runners are billing-blocked, and these local offline commands are
the acceptance source of truth.
