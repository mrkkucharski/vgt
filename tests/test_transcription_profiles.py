from pathlib import Path

import pytest

from vgt.transcription_profiles import (
    ProfileDefinitionError,
    load_project_profiles,
    parse_profiles_toml,
    profiles_path,
    resolve_profile,
    resolved_cleanup_hash,
    resolved_detection_hash,
    resolved_settings_snapshot,
    validate_profile_for_target,
    validate_project_profiles,
)


MINIMAL_PROFILE = """
schema_version = 1

[profiles.my-clean-guitar]
target = "guitar"
extends = "guitar-acoustic-clean"
description = "Clean chord-oriented acoustic reference"
"""


# --- Built-in profile identity (issue #148 acceptance criteria) -----------


def test_detail_and_clean_share_detection_identity_but_differ_in_cleanup() -> None:
    detail = resolve_profile("guitar-acoustic-detail")
    clean = resolve_profile("guitar-acoustic-clean")

    assert resolved_detection_hash(detail) == resolved_detection_hash(clean)
    assert resolved_cleanup_hash(detail) != resolved_cleanup_hash(clean)


def test_strict_chords_has_a_different_detection_identity() -> None:
    clean = resolve_profile("guitar-acoustic-clean")
    strict = resolve_profile("guitar-acoustic-strict-chords")

    assert resolved_detection_hash(clean) != resolved_detection_hash(strict)


def test_guitar_acoustic_alias_matches_clean_resolved_identity() -> None:
    alias = resolve_profile("guitar-acoustic")
    clean = resolve_profile("guitar-acoustic-clean")

    assert resolved_detection_hash(alias) == resolved_detection_hash(clean)
    assert resolved_cleanup_hash(alias) == resolved_cleanup_hash(clean)


def test_unknown_builtin_profile_is_rejected() -> None:
    with pytest.raises(ProfileDefinitionError):
        resolve_profile("guitar-acoustic-does-not-exist")


# --- TOML parsing and validation -------------------------------------------


def test_profiles_path_derives_from_project_path() -> None:
    assert profiles_path("/songs/My Song.RPP") == Path("/songs/My Song.vgt-profiles.toml")


def test_parse_minimal_profile() -> None:
    definitions = parse_profiles_toml(MINIMAL_PROFILE)

    assert set(definitions) == {"my-clean-guitar"}
    defn = definitions["my-clean-guitar"]
    assert defn.target == "guitar"
    assert defn.extends == "guitar-acoustic-clean"
    assert defn.detection == {}
    assert defn.cleanup == {}


def test_parse_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml('schema_version = 1\nbogus = true\n[profiles]\n')


def test_parse_rejects_wrong_schema_version() -> None:
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml("schema_version = 2\n[profiles]\n")


def test_parse_rejects_unknown_profile_key() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
bogus = 1
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_unknown_target() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "kazoo"
extends = "guitar-acoustic-clean"
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_missing_extends() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_unknown_detection_key() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.detection]
bogus = 1
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_out_of_bounds_onset_threshold() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.detection]
onset_threshold = 1.5
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_non_positive_duration() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.cleanup.merge_fragments]
max_gap_s = 0
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_unsupported_cleanup_stage() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.cleanup.force_monophony]
enabled = true
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_parse_rejects_unsupported_cleanup_parameter() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.cleanup.merge_fragments]
bogus = 1
"""
    with pytest.raises(ProfileDefinitionError):
        parse_profiles_toml(text)


def test_load_project_profiles_returns_empty_when_file_missing(tmp_path: Path) -> None:
    project = tmp_path / "Song.RPP"
    assert load_project_profiles(project) == {}


def test_load_project_profiles_reads_sidecar_toml(tmp_path: Path) -> None:
    project = tmp_path / "Song.RPP"
    profiles_path(project).write_text(MINIMAL_PROFILE, encoding="utf-8")

    definitions = load_project_profiles(project)

    assert set(definitions) == {"my-clean-guitar"}


# --- Resolution: inheritance, cycles, target compatibility -----------------


def test_resolve_project_profile_inherits_builtin_detection_and_cleanup() -> None:
    definitions = parse_profiles_toml(MINIMAL_PROFILE)

    resolved = resolve_profile("my-clean-guitar", definitions)

    base = resolve_profile("guitar-acoustic-clean")
    assert resolved.detection == base.detection
    # `clamp_sustain`'s builtin params start empty -- `default_spec_for_target`
    # fills `max_bars` from the detected tempo at spec-build time, which a
    # profile-file resolution has no access to, so it substitutes its own
    # documented default instead (see `_STAGE_DEFAULT_PARAMS`). Every other
    # stage's resolved params, and the stage set/order itself, still match.
    resolved_stage_names = [stage.name for stage in resolved.cleanup]
    base_stage_names = [stage.name for stage in base.cleanup]
    assert resolved_stage_names == base_stage_names
    for resolved_stage, base_stage in zip(resolved.cleanup, base.cleanup):
        if resolved_stage.name == "clamp_sustain":
            continue
        assert resolved_stage.params == base_stage.params
    assert resolved.is_builtin is False
    assert resolved.profile_definition_hash is not None


def test_resolve_project_profile_overrides_only_named_fields() -> None:
    text = """
schema_version = 1
[profiles.tight]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.tight.detection]
onset_threshold = 0.9
"""
    definitions = parse_profiles_toml(text)
    resolved = resolve_profile("tight", definitions)
    base = resolve_profile("guitar-acoustic-clean")

    assert resolved.detection["onset_threshold"] == 0.9
    assert resolved.detection["frame_threshold"] == base.detection["frame_threshold"]


def test_resolve_project_profile_chain_of_project_profiles() -> None:
    text = """
schema_version = 1
[profiles.base-override]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.base-override.detection]
onset_threshold = 0.8

[profiles.child]
target = "guitar"
extends = "base-override"
[profiles.child.detection]
frame_threshold = 0.75
"""
    definitions = parse_profiles_toml(text)
    resolved = resolve_profile("child", definitions)

    assert resolved.detection["onset_threshold"] == 0.8
    assert resolved.detection["frame_threshold"] == 0.75


def test_resolve_detects_inheritance_cycle() -> None:
    text = """
schema_version = 1
[profiles.a]
target = "guitar"
extends = "b"

[profiles.b]
target = "guitar"
extends = "a"
"""
    definitions = parse_profiles_toml(text)
    with pytest.raises(ProfileDefinitionError, match="cycle"):
        resolve_profile("a", definitions)


def test_resolve_rejects_unknown_parent() -> None:
    text = """
schema_version = 1
[profiles.a]
target = "guitar"
extends = "no-such-profile"
"""
    definitions = parse_profiles_toml(text)
    with pytest.raises(ProfileDefinitionError):
        resolve_profile("a", definitions)


def test_resolve_rejects_target_mismatch_within_chain() -> None:
    text = """
schema_version = 1
[profiles.parent]
target = "guitar"
extends = "guitar-acoustic-clean"

[profiles.child]
target = "bass"
extends = "parent"
"""
    definitions = parse_profiles_toml(text)
    with pytest.raises(ProfileDefinitionError):
        resolve_profile("child", definitions)


def test_resolve_rejects_drums_target() -> None:
    text = """
schema_version = 1
[profiles.a]
target = "drums"
extends = "guitar-acoustic-clean"
"""
    definitions = parse_profiles_toml(text)
    with pytest.raises(ProfileDefinitionError):
        resolve_profile("a", definitions)


def test_validate_profile_for_target_rejects_incompatible_target() -> None:
    definitions = parse_profiles_toml(MINIMAL_PROFILE)
    with pytest.raises(ProfileDefinitionError):
        validate_profile_for_target("my-clean-guitar", "bass", definitions)


def test_validate_profile_for_target_accepts_matching_target() -> None:
    definitions = parse_profiles_toml(MINIMAL_PROFILE)
    resolved = validate_profile_for_target("my-clean-guitar", "guitar", definitions)
    assert resolved.name == "my-clean-guitar"


def test_validate_profile_for_target_accepts_builtin_with_no_target_context() -> None:
    resolved = validate_profile_for_target("default", "bass")
    assert resolved.target is None


def test_validate_project_profiles_resolves_every_definition(tmp_path: Path) -> None:
    project = tmp_path / "Song.RPP"
    profiles_path(project).write_text(MINIMAL_PROFILE, encoding="utf-8")

    resolved = validate_project_profiles(project)

    assert set(resolved) == {"my-clean-guitar"}


def test_validate_project_profiles_surfaces_a_cycle_before_any_backend_use(tmp_path: Path) -> None:
    project = tmp_path / "Song.RPP"
    text = """
schema_version = 1
[profiles.a]
target = "guitar"
extends = "b"

[profiles.b]
target = "guitar"
extends = "a"
"""
    profiles_path(project).write_text(text, encoding="utf-8")

    with pytest.raises(ProfileDefinitionError):
        validate_project_profiles(project)


# --- Settings identity: only changed values change hashes ------------------


def test_editing_an_inherited_profile_changes_only_the_hash_whose_value_changed() -> None:
    base = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.detection]
onset_threshold = 0.61
"""
    edited_cleanup_only = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.detection]
onset_threshold = 0.61
[profiles.x.cleanup.clamp_sustain]
enabled = true
max_bars = 3.0
"""
    base_resolved = resolve_profile("x", parse_profiles_toml(base))
    edited_resolved = resolve_profile("x", parse_profiles_toml(edited_cleanup_only))

    assert resolved_detection_hash(base_resolved) == resolved_detection_hash(edited_resolved)
    assert resolved_cleanup_hash(base_resolved) != resolved_cleanup_hash(edited_resolved)


def test_spectral_fft_and_hop_length_are_hash_visible() -> None:
    base = resolve_profile("guitar-acoustic-clean")
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.cleanup.drop_harmonic_ghosts]
enabled = true
spectral_n_fft = 8192
spectral_hop_length = 1024
"""
    resolved = resolve_profile("x", parse_profiles_toml(text))

    assert resolved_detection_hash(resolved) == resolved_detection_hash(base)
    assert resolved_cleanup_hash(resolved) != resolved_cleanup_hash(base)


def test_disabling_a_stage_changes_the_cleanup_hash_and_drops_it_from_the_snapshot() -> None:
    base = resolve_profile("guitar-acoustic-clean")
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.cleanup.drop_harmonic_ghosts]
enabled = false
"""
    resolved = resolve_profile("x", parse_profiles_toml(text))
    snapshot = resolved_settings_snapshot(resolved)

    assert resolved_cleanup_hash(resolved) != resolved_cleanup_hash(base)
    assert all(stage["name"] != "drop_harmonic_ghosts" for stage in snapshot["cleanup"])


def test_resolved_cleanup_preserves_canonical_stage_order_regardless_of_declaration_order() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.cleanup.cap_simultaneous_voices]
enabled = true
max_voices = 4
min_duration_after_cap_s = 0.04
[profiles.x.cleanup.merge_fragments]
enabled = true
max_gap_s = 0.02
"""
    resolved = resolve_profile("x", parse_profiles_toml(text))

    names = [stage.name for stage in resolved.cleanup]
    assert names == sorted(names, key=lambda name: (
        "merge_fragments", "drop_isolated_notes", "clamp_sustain",
        "drop_harmonic_ghosts", "cap_simultaneous_voices",
    ).index(name))


def test_resolve_rejects_minimum_frequency_not_below_maximum() -> None:
    text = """
schema_version = 1
[profiles.x]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.x.detection]
minimum_frequency_hz = 1200
maximum_frequency_hz = 80
"""
    definitions = parse_profiles_toml(text)
    with pytest.raises(ProfileDefinitionError):
        resolve_profile("x", definitions)
