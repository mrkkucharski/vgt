from pathlib import Path
import collections
import hashlib
import json
import struct

import pytest

from vgt.transcribe import (
    BasicPitchSpec,
    AdtofSpec,
    ADTOF_PACKAGE_VERSION,
    ADTOF_PEAK_THRESHOLDS,
    ADTOF_MIN_INTER_ONSET_SECONDS,
    AdtofTranscriber,
    ADTOF_MODEL_VERSION,
    ADTOF_WEIGHTS_VERSION,
    BasicPitchTranscriber,
    CleanupStage,
    DrumScriptSpec,
    DrumScriptTranscriber,
    GUITAR_GHOST_ONSET_TOLERANCE_S,
    GUITAR_GHOST_OVERLAP_FRACTION,
    GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
    GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
    GUITAR_GHOST_SPECTRAL_N_FFT,
    GUITAR_GHOST_SPECTRAL_HOP_LENGTH,
    GUITAR_GHOST_VELOCITY_SLACK,
    GUITAR_MAX_SIMULTANEOUS_VOICES,
    GUITAR_HARMONIC_GHOST_INTERVALS,
    GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    VALID_TARGETS,
    FakeTranscriber,
    FakeAdtofTranscriber,
    PYIN_ALGORITHM_VERSION,
    ParsedNote,
    PyinSpec,
    PyinTranscriber,
    TargetTranscriberRouter,
    TranscriptionError,
    _apply_cleanup_stages,
    _bar_duration_seconds,
    _cap_simultaneous_voices,
    _clamp_sustain,
    _drop_harmonic_ghosts,
    _drop_isolated_notes,
    _force_monophony,
    _load_spectral_analysis,
    _merge_fragments,
    _midi_to_hz,
    default_spec_for_target,
    drum_transcription_profile,
    midi_artifact_name,
    missing_source_entry,
    notes_artifact_name,
    resolve_target_source,
    spec_hash,
    production_transcriber_router,
    target_input_hash,
    transcribed_entry,
    validate_target,
)


def _cleanup_names(spec: BasicPitchSpec) -> list[str]:
    return [stage.name for stage in spec.cleanup]


def _cleanup_params(spec: BasicPitchSpec, name: str) -> dict:
    return next(stage.params for stage in spec.cleanup if stage.name == name)


def _write_source(tmp_path: Path, name: str = "guitar.wav", content: bytes = b"fake-audio-bytes") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_default_spec_applies_the_per_target_frequency_table() -> None:
    guitar = default_spec_for_target("guitar")
    bass = default_spec_for_target("bass")
    legacy_bass = default_spec_for_target("bass", modes={"bass": "bass-basic-pitch"})
    vocals = default_spec_for_target("vocals")
    piano = default_spec_for_target("piano")

    assert (guitar.minimum_frequency_hz, guitar.maximum_frequency_hz) == (80.0, 1200.0)
    # Bass's window is the pyin tracker's fundamental search range, narrowed to
    # the range two independent estimators measured on a real stem; the retired
    # Basic Pitch profile keeps its original, wider window.
    assert (bass.minimum_frequency_hz, bass.maximum_frequency_hz) == (35.0, 330.0)
    assert (legacy_bass.minimum_frequency_hz, legacy_bass.maximum_frequency_hz) == (30.0, 400.0)
    assert (vocals.minimum_frequency_hz, vocals.maximum_frequency_hz) == (70.0, 1200.0)
    # Polyphonic/unpredictable targets get Basic Pitch's own full-range defaults.
    assert (piano.minimum_frequency_hz, piano.maximum_frequency_hz) == (None, None)


def test_guitar_harmonic_is_default_and_explicit_default_is_raw_opt_out() -> None:
    """Stored stale modes safely use the harmonic default."""
    unset = default_spec_for_target("guitar")
    electric = default_spec_for_target("guitar", modes={"guitar": "electric"})
    raw = default_spec_for_target("guitar", modes={"guitar": "default"})

    for spec in (unset, electric):
        assert (spec.minimum_frequency_hz, spec.maximum_frequency_hz) == (80.0, 1200.0)
        assert spec.onset_threshold == 0.6
        assert spec.frame_threshold == 0.65
        assert "drop_harmonic_ghosts" in _cleanup_names(spec)
    assert (raw.minimum_frequency_hz, raw.maximum_frequency_hz) == (70.0, 1400.0)
    assert raw.cleanup == ()


def test_default_spec_narrows_acoustic_guitar_and_enables_cleanup() -> None:
    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=120.0)

    assert (spec.minimum_frequency_hz, spec.maximum_frequency_hz) == (80.0, 1200.0)
    assert spec.onset_threshold == 0.6
    assert spec.frame_threshold == 0.65
    assert spec.minimum_note_length_ms == 100.0
    assert spec.melodia_trick is False
    assert _cleanup_names(spec) == [
        "merge_fragments",
        "drop_isolated_notes",
        "clamp_sustain",
        "drop_harmonic_ghosts",
        "cap_simultaneous_voices",
    ]
    assert _cleanup_params(spec, "cap_simultaneous_voices")["max_voices"] == GUITAR_MAX_SIMULTANEOUS_VOICES
    assert _cleanup_params(spec, "cap_simultaneous_voices")["min_duration_after_cap_s"] == GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S
    assert _cleanup_params(spec, "merge_fragments")["max_gap_s"] == pytest.approx(0.03)
    assert _cleanup_params(spec, "drop_harmonic_ghosts") == {
        "intervals": GUITAR_HARMONIC_GHOST_INTERVALS,
        "onset_tolerance_s": GUITAR_GHOST_ONSET_TOLERANCE_S,
        "overlap_fraction": GUITAR_GHOST_OVERLAP_FRACTION,
        "velocity_slack": GUITAR_GHOST_VELOCITY_SLACK,
        "spectral_n_fft": GUITAR_GHOST_SPECTRAL_N_FFT,
        "spectral_hop_length": GUITAR_GHOST_SPECTRAL_HOP_LENGTH,
        "spectral_max_harmonic_order": GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
        "spectral_freq_tolerance_semitones": GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
        "spectral_independent_energy_ratio": GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    }
    # Two bars at 120 BPM 4/4: 2 * 4 beats * 60/120 = 4.0s.
    assert _cleanup_params(spec, "clamp_sustain")["max_duration_s"] == pytest.approx(4.0)


def test_default_spec_mode_override_is_target_local() -> None:
    vocals = default_spec_for_target("vocals", modes={"guitar": "guitar-acoustic"})

    assert (vocals.minimum_frequency_hz, vocals.maximum_frequency_hz) == (70.0, 1200.0)
    assert vocals.cleanup == ()


def test_bass_defaults_to_the_monophonic_pyin_tracker() -> None:
    """Bass's default is a monophonic pitch tracker, not Basic Pitch.

    Basic Pitch cannot produce a usable bass line at any setting -- see
    `vgt.pyin_notes` for the measurement. The two Basic Pitch bass profiles
    remain reachable by explicit selection so the old behaviour can still be
    compared against, and must keep resolving to a `BasicPitchSpec`.
    """
    default = default_spec_for_target("bass", midi_tempo=120.0, time_signature="4/4")

    assert isinstance(default, PyinSpec)
    assert default.backend == "pyin"
    assert default.algorithm_version == PYIN_ALGORITHM_VERSION
    # No ghost-dropping or voice-capping stage: a tracker emits one line by
    # construction, so those stages would have nothing to do.
    assert _cleanup_names(default) == ["merge_fragments", "drop_isolated_notes", "clamp_sustain"]
    # Two bars at 120 BPM 4/4.
    assert _cleanup_params(default, "clamp_sustain")["max_duration_s"] == pytest.approx(4.0)

    for name in ("bass-basic-pitch", "bass-monophonic"):
        spec = default_spec_for_target("bass", modes={"bass": name})
        assert isinstance(spec, BasicPitchSpec), name
        assert (spec.minimum_frequency_hz, spec.maximum_frequency_hz) == (30.0, 400.0), name
    assert _cleanup_names(default_spec_for_target("bass", modes={"bass": "bass-monophonic"})) == ["force_monophony"]
    assert default_spec_for_target("bass", modes={"bass": "bass-basic-pitch"}).cleanup == ()


def test_pyin_spec_omits_the_basic_pitch_only_settings_it_never_reads() -> None:
    """A pyin variant's identity must not contain fields nothing consults.

    This is the whole reason `PyinSpec` exists instead of reusing
    `BasicPitchSpec` with a different `backend` string: an `onset_threshold` in
    the hash would invite a project profile to "tune" a no-op.
    """
    payload = default_spec_for_target("bass", midi_tempo=120.0).to_dict()

    for absent in ("onset_threshold", "frame_threshold", "melodia_trick", "multiple_pitch_bends", "package_pin", "serialization"):
        assert absent not in payload, absent
    for present in ("algorithm_version", "sample_rate_hz", "hop_length", "median_filter_frames"):
        assert present in payload, present


def test_default_spec_ignores_a_stored_profile_for_another_target() -> None:
    """A stale or malformed sidecar mode must retain the target default."""
    bass = default_spec_for_target("bass", modes={"bass": "guitar-acoustic"})

    assert isinstance(bass, PyinSpec)
    assert (bass.minimum_frequency_hz, bass.maximum_frequency_hz) == (35.0, 330.0)


def test_default_spec_acoustic_sustain_clamp_scales_with_time_signature() -> None:
    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=120.0, time_signature="3/4")

    # 2 bars * 3 beats * 60/120 = 3.0s.
    assert _cleanup_params(spec, "clamp_sustain")["max_duration_s"] == pytest.approx(3.0)


def test_default_spec_acoustic_sustain_clamp_is_none_without_a_tempo() -> None:
    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=None)

    assert "clamp_sustain" not in _cleanup_names(spec)
    # The rest of the acoustic override still applies.
    assert "drop_harmonic_ghosts" in _cleanup_names(spec)


def test_bar_duration_seconds_defaults_to_4_4_when_signature_is_missing_or_malformed() -> None:
    assert _bar_duration_seconds(120.0, None) == pytest.approx(2.0)
    assert _bar_duration_seconds(120.0, "garbage") == pytest.approx(2.0)
    assert _bar_duration_seconds(None, "4/4") is None


def test_default_spec_shares_the_common_defaults_across_targets() -> None:
    guitar = default_spec_for_target("guitar")
    vocals = default_spec_for_target("vocals")

    assert guitar.backend == vocals.backend == "basic-pitch"
    assert guitar.minimum_note_length_ms == 100.0
    assert vocals.minimum_note_length_ms == 60.0
    assert guitar.melodia_trick is False
    assert guitar.multiple_pitch_bends is False


def test_guitar_and_bass_specs_hash_differently() -> None:
    guitar = default_spec_for_target("guitar")
    bass = default_spec_for_target("bass")

    assert spec_hash(guitar) != spec_hash(bass)


def test_spec_hash_is_deterministic_and_order_independent() -> None:
    spec = default_spec_for_target("guitar", midi_tempo=118.02)

    assert spec_hash(spec) == spec_hash(default_spec_for_target("guitar", midi_tempo=118.02))


def test_spec_hash_changes_when_a_target_setting_changes() -> None:
    from dataclasses import replace

    spec = default_spec_for_target("guitar")
    retuned = replace(spec, onset_threshold=0.7)

    assert spec_hash(spec) != spec_hash(retuned)


def test_spec_hash_is_unchanged_for_every_target_without_a_cleanup_pipeline() -> None:
    """The `cleanup` tuple replaced five always-present boolean/float fields
    on `BasicPitchSpec` (`max_simultaneous_voices`, `sustain_clamp_s`,
    `drop_harmonic_ghosts`, `merge_gap_s`, `drop_isolated_notes`). Swapping a
    dataclass field for a new one changes every instance's serialized shape,
    which would silently move `settings_hash` for every basic-pitch target,
    not just guitar-acoustic. `BasicPitchSpec.to_dict` reproduces the old
    five-field shape whenever `cleanup` is empty specifically to prevent
    that -- this pins the resulting hash against a hand-built pre-refactor
    dict so a regression here (e.g. someone dropping the shim) is caught.

    `bass` is covered through its explicit `bass-basic-pitch` profile rather
    than as a bare target: bass now defaults to the pyin backend, whose
    `PyinSpec` has no pre-refactor shape to preserve. Keeping the retired
    Basic Pitch bass profile in this list still proves that moving bass's
    default did not disturb the legacy hash.
    """
    cases: list[tuple[str, dict[str, str] | None]] = [
        (target, None)
        for target in ("vocals", "piano", "strings", "instrumental", "backing", "original")
    ]
    cases.append(("guitar", {"guitar": "default"}))
    cases.append(("bass", {"bass": "bass-basic-pitch"}))
    for target, modes in cases:
        spec = default_spec_for_target(target, modes=modes)
        assert spec.cleanup == ()
        pre_refactor_dict = {
            "backend": spec.backend,
            "package_pin": spec.package_pin,
            "serialization": spec.serialization,
            "onset_threshold": spec.onset_threshold,
            "frame_threshold": spec.frame_threshold,
            "minimum_note_length_ms": spec.minimum_note_length_ms,
            "minimum_frequency_hz": spec.minimum_frequency_hz,
            "maximum_frequency_hz": spec.maximum_frequency_hz,
            "multiple_pitch_bends": spec.multiple_pitch_bends,
            "melodia_trick": spec.melodia_trick,
            "midi_tempo": spec.midi_tempo,
            "max_simultaneous_voices": None,
            "sustain_clamp_s": None,
            "drop_harmonic_ghosts": False,
            "merge_gap_s": None,
            "drop_isolated_notes": False,
        }
        pre_refactor_hash = hashlib.sha256(json.dumps(pre_refactor_dict, sort_keys=True).encode("utf-8")).hexdigest()
        assert spec_hash(spec) == pre_refactor_hash, target


def test_spec_hash_changes_when_a_cleanup_stage_parameter_changes() -> None:
    """Regression test for the bug this profile refactor exists to close: a
    cleanup stage's tuning constant (e.g. `GUITAR_ISOLATED_MAX_DURATION_S`)
    used to be read directly from module scope inside the cleanup function,
    invisible to `settings_hash` -- retuning it left every cached
    transcription silently stale. Now every stage parameter lives in
    `spec.cleanup`, so it always flows into the hash."""
    from dataclasses import replace

    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=120.0)
    isolated_stage_index = next(i for i, stage in enumerate(spec.cleanup) if stage.name == "drop_isolated_notes")
    retuned_stage = replace(
        spec.cleanup[isolated_stage_index],
        params={**spec.cleanup[isolated_stage_index].params, "max_duration_s": 0.2},
    )
    retuned_cleanup = spec.cleanup[:isolated_stage_index] + (retuned_stage,) + spec.cleanup[isolated_stage_index + 1 :]
    retuned = replace(spec, cleanup=retuned_cleanup)

    assert spec_hash(spec) != spec_hash(retuned)


def test_drumscript_spec_identity_covers_every_drumscript_setting() -> None:
    from dataclasses import replace

    spec = default_spec_for_target(
        "drums", backend="drumscript", drumscript_time_signature=(7, 8)
    )
    assert isinstance(spec, DrumScriptSpec)
    assert spec.package_pin == "drumscript==0.1.6"
    assert spec.runtime_version == "python==3.12"
    assert spec.classifier_mode == "standard-polyphonic"
    assert spec.time_signature == (7, 8)
    assert spec_hash(spec) != spec_hash(replace(spec, classifier_mode="rudiment"))
    assert spec_hash(spec) != spec_hash(replace(spec, runtime_version="python==3.11"))
    assert spec_hash(spec) != spec_hash(replace(spec, time_signature=(4, 4)))


def test_drumscript_uses_gentle_hpss_by_default_and_explicit_default_is_raw_opt_out() -> None:
    unset = default_spec_for_target("drums", backend="drumscript")
    explicit_default = default_spec_for_target("drums", backend="drumscript", modes={"drums": "default"})
    stale = default_spec_for_target("drums", backend="drumscript", modes={"drums": "not-a-real-profile"})

    assert unset.cleanup_profile == "drums-clean"
    assert len(unset.cleanup) == 1
    assert explicit_default.cleanup_profile == "default"
    assert explicit_default.cleanup == ()
    assert spec_hash(unset) == spec_hash(stale)
    assert spec_hash(unset) != spec_hash(explicit_default)


def test_drumscript_clean_profile_shares_the_gentle_defaults_backend_spec() -> None:
    default_spec = default_spec_for_target("drums", backend="drumscript")
    clean_spec = default_spec_for_target("drums", backend="drumscript", modes={"drums": "drums-clean"})

    assert clean_spec.cleanup_profile == "drums-clean"
    assert len(clean_spec.cleanup) == 1
    assert clean_spec.cleanup[0].name == "drums-clean"
    # The frontend is intentionally outside the DrumScript spec: the variant
    # layer hashes it with the raw input before invoking this unchanged backend.
    assert spec_hash(clean_spec) == spec_hash(default_spec)
    # Every other target's identity is untouched by drums selecting a clean profile.
    guitar_spec = default_spec_for_target("guitar", modes={"drums": "drums-clean"})
    assert guitar_spec.to_dict() == default_spec_for_target("guitar").to_dict()


def test_every_drumscript_profile_carries_the_analysis_beat_grid_in_its_identity() -> None:
    """Both profiles author onto the analyzed grid, so both must carry it --
    and a re-analysis that moves the grid must change the settings hash rather
    than silently reusing MIDI aligned to the old one."""
    default_spec = default_spec_for_target(
        "drums", backend="drumscript", beat_times=(0.0853, 0.58528), downbeat_offset_s=0.0853,
    )
    clean_spec = default_spec_for_target(
        "drums", backend="drumscript", modes={"drums": "drums-clean"},
        beat_times=(0.0853, 0.58528), downbeat_offset_s=0.0853,
    )
    moved_grid = default_spec_for_target(
        "drums", backend="drumscript", beat_times=(0.2, 0.7), downbeat_offset_s=0.2,
    )

    for spec in (default_spec, clean_spec):
        assert spec.beat_grid is not None
        assert spec.beat_grid.beat_times == pytest.approx((0.0853, 0.58528))
        assert spec.beat_grid.downbeat_offset_s == pytest.approx(0.0853)
        assert spec.to_dict()["beat_grid"]["downbeat_offset_s"] == pytest.approx(0.0853)
    assert spec_hash(moved_grid) != spec_hash(default_spec)
    assert spec_hash(default_spec) == spec_hash(clean_spec)


def test_drumscript_clean_profile_name_is_valid_for_the_drums_target() -> None:
    from vgt.transcribe import effective_profile_name_for_target, valid_profile_names_for_target, validate_profile_for_target

    assert valid_profile_names_for_target("drums") == ("default", "drums-clean", "drums-adtof", "drums-hpss-gentle")
    assert validate_profile_for_target("drums", "drums-clean") == "drums-clean"
    assert validate_profile_for_target("drums", "drums-adtof") == "drums-adtof"
    with pytest.raises(TranscriptionError):
        validate_profile_for_target("drums", "not-a-real-profile")

    assert effective_profile_name_for_target("drums", None) == "drums-hpss-gentle"
    assert effective_profile_name_for_target("drums", {"drums": "drums-clean"}) == "drums-clean"
    assert effective_profile_name_for_target("drums", {"drums": "not-a-real-profile"}) == "drums-hpss-gentle"


def test_router_routes_only_drums_to_an_injected_drum_backend() -> None:
    class FakeBasicPitch(FakeTranscriber):
        name = "basic-pitch"

    class FakeDrumScript(FakeTranscriber):
        name = "drumscript"

    basic_pitch = FakeBasicPitch()
    drumscript = FakeDrumScript()
    router = TargetTranscriberRouter(basic_pitch, drumscript, drumscript_targets=("drums",))

    for target in VALID_TARGETS:
        assert router.for_target(target) is (drumscript if target == "drums" else basic_pitch)
    assert isinstance(router.spec_for_target("drums", midi_tempo=120.0), DrumScriptSpec)
    assert isinstance(router.spec_for_target("guitar", midi_tempo=120.0), BasicPitchSpec)


def test_router_uses_the_drum_profile_backend_and_keeps_drumscript_default() -> None:
    class FakeDrumScript(FakeTranscriber):
        name = "drumscript"

    drumscript = FakeDrumScript()
    adtof = FakeAdtofTranscriber()
    router = TargetTranscriberRouter(FakeTranscriber(), drumscript, drumscript_targets=("drums",), adtof=adtof)

    assert router.for_target("drums") is drumscript
    assert drum_transcription_profile({"drums": "default"}).backend == "drumscript"
    assert drum_transcription_profile({"drums": "drums-adtof"}).backend == "adtof"
    assert router.for_target("drums", {"drums": "drums-adtof"}) is adtof
    spec = router.spec_for_target(
        "drums", midi_tempo=120.0, modes={"drums": "drums-adtof"}
    )
    assert isinstance(spec, AdtofSpec)
    assert spec.to_dict() == {
        "backend": "adtof",
        "package_pin": (
            "adtof-pytorch @ git+https://github.com/xavriley/ADTOF-pytorch.git@"
            "85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"
        ),
        "package_version": ADTOF_PACKAGE_VERSION,
        "model_version": ADTOF_MODEL_VERSION,
        "weights_version": ADTOF_WEIGHTS_VERSION,
        "weights_sha256": "1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320",
        "runtime_version": "python==3.11",
        "torch_version": "torch==2.13.0",
        "lock_sha256": "c1c0e70cd0ff9f3045536a49940d9a9e8ada6523bd17424c36fd4f40e5ebb3e2",
        "midi_tempo": 120.0,
        "beat_grid": None,
        "peak_thresholds": ADTOF_PEAK_THRESHOLDS,
        "min_inter_onset_seconds": ADTOF_MIN_INTER_ONSET_SECONDS,
        "grid_subdivisions": 2,
    }


def test_router_threads_modes_and_time_signature_through_to_the_spec() -> None:
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber())

    spec = router.spec_for_target("guitar", midi_tempo=120.0, modes={"guitar": "guitar-acoustic"}, time_signature="3/4")

    assert "drop_harmonic_ghosts" in _cleanup_names(spec)
    assert _cleanup_params(spec, "clamp_sustain")["max_duration_s"] == pytest.approx(3.0)


def test_production_router_sends_drums_to_drumscript_bass_to_pyin_and_the_rest_to_basic_pitch() -> None:
    router = production_transcriber_router()

    for target in VALID_TARGETS:
        if target == "drums":
            assert router.for_target(target).name == "drumscript"
            assert isinstance(router.for_target(target), DrumScriptTranscriber)
        elif target == "bass":
            assert router.for_target(target).name == "pyin"
            assert isinstance(router.for_target(target), PyinTranscriber)
        else:
            assert router.for_target(target).name == "basic-pitch"
            assert isinstance(router.for_target(target), BasicPitchTranscriber)
    assert isinstance(router.spec_for_target("bass", midi_tempo=120.0), PyinSpec)
    # An explicit Basic Pitch bass profile routes back to Basic Pitch, so the
    # backend follows the profile rather than the target name.
    assert router.for_target("bass", {"bass": "bass-monophonic"}).name == "basic-pitch"
    assert isinstance(router.spec_for_target("drums", midi_tempo=120.0), DrumScriptSpec)
    assert router.for_target("drums", {"drums": "drums-adtof"}).name == "adtof"
    assert isinstance(router.for_target("drums", {"drums": "drums-adtof"}), AdtofTranscriber)
    assert isinstance(router.spec_for_target("drums", midi_tempo=120.0, modes={"drums": "drums-adtof"}), AdtofSpec)
    assert isinstance(router.spec_for_target("guitar", midi_tempo=120.0), BasicPitchSpec)


def test_validate_target_accepts_every_documented_target() -> None:
    for target in VALID_TARGETS:
        assert validate_target(target) == target


def test_validate_target_rejects_an_unknown_name() -> None:
    with pytest.raises(TranscriptionError, match="target must be one of"):
        validate_target("kazoo")


def test_default_spec_for_target_rejects_an_unknown_name() -> None:
    with pytest.raises(TranscriptionError):
        default_spec_for_target("kazoo")


def test_artifact_names_are_namespaced_per_target_to_avoid_collisions() -> None:
    assert midi_artifact_name("guitar") == "transcription/guitar.mid"
    assert notes_artifact_name("guitar") == "transcription/guitar.csv"
    assert midi_artifact_name("bass") == "transcription/bass.mid"
    assert notes_artifact_name("bass") == "transcription/bass.csv"


def test_artifact_name_helpers_reject_an_unknown_target() -> None:
    with pytest.raises(TranscriptionError):
        midi_artifact_name("kazoo")
    with pytest.raises(TranscriptionError):
        notes_artifact_name("kazoo")


def _read_midi_track_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    assert data[:4] == b"MThd"
    header_len = struct.unpack(">I", data[4:8])[0]
    assert header_len == 6
    track_start = 8 + header_len
    assert data[track_start:track_start + 4] == b"MTrk"
    track_len = struct.unpack(">I", data[track_start + 4:track_start + 8])[0]
    track_bytes = data[track_start + 8:track_start + 8 + track_len]
    assert len(track_bytes) == track_len
    assert track_bytes[-3:] == bytes([0xFF, 0x2F, 0x00])  # end-of-track meta event
    return track_bytes


def test_fake_transcriber_round_trip_writes_a_valid_midi_and_matching_csv(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    destination = tmp_path / "out"
    spec = default_spec_for_target("guitar", backend="fake", midi_tempo=120.0)

    result = FakeTranscriber().transcribe(source, destination, spec)

    assert result.midi_path.is_file()
    assert result.notes_path.is_file()
    assert result.note_count > 0
    assert result.pitch_range_midi is not None
    low, high = result.pitch_range_midi
    assert low <= high
    assert result.first_note_s is not None
    assert result.last_note_s is not None
    assert result.last_note_s > result.first_note_s

    _read_midi_track_bytes(result.midi_path)  # raises on structural problems

    lines = result.notes_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend"
    assert len(lines) - 1 == result.note_count
    # The CSV quirk this stage must tolerate: pitch_bend is a variable-length
    # trailing sequence, so rows are not required to have the same column
    # count as each other.
    column_counts = {len(line.split(",")) for line in lines[1:]}
    assert min(column_counts) >= 4


def test_fake_transcriber_is_content_addressed(tmp_path: Path) -> None:
    """Same source bytes and spec reliably reproduce the same notes (needed
    for future cache-hit tests); different source bytes reliably change them
    (needed for future cache-invalidation tests)."""
    spec = default_spec_for_target("guitar", backend="fake")
    source_a = _write_source(tmp_path, "a.wav", b"content-a")
    source_b = _write_source(tmp_path, "b.wav", b"content-b")

    first = FakeTranscriber().transcribe(source_a, tmp_path / "out1", spec)
    second = FakeTranscriber().transcribe(source_a, tmp_path / "out2", spec)
    third = FakeTranscriber().transcribe(source_b, tmp_path / "out3", spec)

    assert first.note_count == second.note_count
    assert first.pitch_range_midi == second.pitch_range_midi
    assert first.first_note_s == second.first_note_s
    assert first.last_note_s == second.last_note_s
    assert (first.pitch_range_midi, first.last_note_s) != (third.pitch_range_midi, third.last_note_s)


def test_fake_transcriber_respects_the_per_target_frequency_bounds(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    bass_spec = default_spec_for_target("bass", backend="fake")

    result = FakeTranscriber().transcribe(source, tmp_path / "out", bass_spec)

    # Bass's narrow 30-400 Hz band corresponds to roughly MIDI 22-67; a fake
    # note far outside that band would indicate the spec bounds were ignored.
    low, high = result.pitch_range_midi
    assert 10 <= low <= 70
    assert 10 <= high <= 70


def test_resolve_target_source_finds_a_recorded_stem_artifact(tmp_path: Path) -> None:
    project_path = tmp_path / "Song.RPP"
    stem_dir = tmp_path / "vgt" / "ns1"
    stem_dir.mkdir(parents=True)
    stem_file = stem_dir / "guitar.wav"
    stem_file.write_bytes(b"stem-audio")
    analysis = {"stems": {"artifacts": {"guitar": {"file": "vgt/ns1/guitar.wav", "sha256": "deadbeef"}}}}
    reference_source = tmp_path / "mix.wav"
    reference_source.write_bytes(b"mix-audio")

    resolved = resolve_target_source(project_path, "guitar", analysis, reference_source=reference_source)

    assert resolved is not None
    path, artifact = resolved
    assert path == stem_file
    assert artifact == {"file": "vgt/ns1/guitar.wav", "sha256": "deadbeef"}


def test_resolve_target_source_is_none_for_a_missing_stem(tmp_path: Path) -> None:
    project_path = tmp_path / "Song.RPP"
    analysis = {"stems": {"artifacts": {}}}
    reference_source = tmp_path / "mix.wav"
    reference_source.write_bytes(b"mix-audio")

    resolved = resolve_target_source(project_path, "bass", analysis, reference_source=reference_source)

    assert resolved is None


def test_resolve_target_source_never_falls_back_to_the_mix_for_a_stem_target(tmp_path: Path) -> None:
    """A missing stem must resolve to `None`, never silently to the mix --
    transcribing the full mix and labelling it e.g. a guitar reference would
    be worse than producing nothing."""
    project_path = tmp_path / "Song.RPP"
    analysis: dict = {"stems": {"artifacts": {}}}
    reference_source = tmp_path / "mix.wav"
    reference_source.write_bytes(b"mix-audio")

    assert resolve_target_source(project_path, "guitar", analysis, reference_source=reference_source) is None


def test_resolve_target_source_original_is_the_reference_mix(tmp_path: Path) -> None:
    project_path = tmp_path / "Song.RPP"
    reference_source = tmp_path / "mix.wav"
    reference_source.write_bytes(b"mix-audio")

    resolved = resolve_target_source(project_path, "original", {}, reference_source=reference_source)

    assert resolved == (reference_source, None)


def test_missing_source_produces_a_retained_skipped_entry() -> None:
    spec = default_spec_for_target("bass", backend="fake")

    entry = missing_source_entry(spec, "original")

    assert entry["status"] == "skipped-missing-source"
    assert entry["midi_file"] is None
    assert entry["notes_file"] is None
    assert entry["note_count"] is None
    assert entry["input_hash"] is None
    assert entry["settings_hash"] == spec_hash(spec)
    assert entry["error"] is None


def test_target_input_hash_prefers_the_stem_artifacts_recorded_sha256(tmp_path: Path) -> None:
    stem_file = tmp_path / "guitar.wav"
    stem_file.write_bytes(b"stem-audio")

    assert target_input_hash(stem_file, {"sha256": "deadbeef"}) == "deadbeef"


def test_target_input_hash_falls_back_to_hash_source_file_without_an_artifact(tmp_path: Path) -> None:
    stem_file = tmp_path / "mix.wav"
    stem_file.write_bytes(b"mix-audio")

    first = target_input_hash(stem_file, None)
    second = target_input_hash(stem_file, None)

    assert first == second
    assert first  # non-empty


def test_transcribed_entry_records_the_fake_transcribers_result(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    spec = default_spec_for_target("guitar", backend="fake", midi_tempo=118.02)
    result = FakeTranscriber().transcribe(source, tmp_path / "out", spec)

    entry = transcribed_entry(
        spec,
        source_role="guitar",
        input_hash="deadbeef",
        target="guitar",
        result=result,
        transcribed_at="2026-07-21T00:00:00Z",
    )

    assert entry["status"] == "transcribed"
    assert entry["midi_file"] == "transcription/guitar.mid"
    assert entry["notes_file"] == "transcription/guitar.csv"
    assert entry["note_count"] == result.note_count
    assert entry["pitch_range_midi"] == list(result.pitch_range_midi)
    assert entry["input_hash"] == "deadbeef"
    assert entry["settings_hash"] == spec_hash(spec)
    assert entry["midi_tempo"] == 118.02
    assert entry["error"] is None


# ---------------------------------------------------------------------------
# acoustic-guitar cleanup (see docs/guitar-transcription-findings.md)
# ---------------------------------------------------------------------------


def _note(start_s: float, end_s: float, pitch_midi: int, velocity: int = 90) -> ParsedNote:
    return ParsedNote(start_s, end_s, pitch_midi, velocity, ())


def test_merge_fragments_rejoins_a_note_split_in_place() -> None:
    """The dominant artifact: 390 of the reference track's same-pitch gaps
    were exactly zero-width, i.e. one note emitted as two."""
    first = _note(1.0, 1.5, 60)
    second = _note(1.5, 2.0, 60)  # zero-width gap

    merged = _merge_fragments([first, second], max_gap_s=0.03)

    assert len(merged) == 1
    assert merged[0].start_s == pytest.approx(1.0)
    assert merged[0].end_s == pytest.approx(2.0)


def test_merge_fragments_collapses_a_chain_in_one_pass() -> None:
    chain = [_note(0.0, 0.5, 60), _note(0.51, 1.0, 60), _note(1.02, 1.5, 60), _note(1.5, 2.0, 60)]

    merged = _merge_fragments(chain, max_gap_s=0.03)

    assert len(merged) == 1
    assert merged[0].end_s == pytest.approx(2.0)


def test_merge_fragments_leaves_a_genuine_rearticulation_alone() -> None:
    first = _note(0.0, 0.5, 60)
    second = _note(1.0, 1.5, 60)  # half-second gap: a real repeated note

    merged = _merge_fragments([first, second], max_gap_s=0.03)

    assert len(merged) == 2


def test_merge_fragments_never_joins_different_pitches() -> None:
    merged = _merge_fragments([_note(0.0, 0.5, 60), _note(0.5, 1.0, 61)], max_gap_s=0.03)

    assert len(merged) == 2


def test_merge_fragments_keeps_the_loudest_fragments_velocity() -> None:
    """A split note's later fragment can carry the true peak; keeping the max
    also stops a reassembled note being retired by the voice cap for looking
    quiet."""
    merged = _merge_fragments([_note(0.0, 0.5, 60, velocity=40), _note(0.5, 1.0, 60, velocity=100)], max_gap_s=0.03)

    assert merged[0].velocity == 100


def test_merge_fragments_handles_an_overlapping_pair_without_shortening_it() -> None:
    """Basic Pitch can emit same-pitch notes that overlap rather than abut;
    merging must take the later end time, never truncate to the nearer one."""
    merged = _merge_fragments([_note(0.0, 2.0, 60), _note(0.5, 1.0, 60)], max_gap_s=0.03)

    assert len(merged) == 1
    assert merged[0].end_s == pytest.approx(2.0)


def test_drop_isolated_notes_removes_a_lone_blip() -> None:
    blip = _note(30.0, 30.05, 77)
    company = [_note(1.0, 1.5, 60), _note(2.0, 2.5, 60)]

    kept = _drop_isolated_notes([*company, blip], max_duration_s=0.15, neighbour_window_s=1.0)

    assert blip not in kept
    assert all(note in kept for note in company)


def test_drop_isolated_notes_keeps_a_short_note_that_has_company_at_its_pitch() -> None:
    """A short note inside a run at the same pitch is a played note, not a blip."""
    run = [_note(1.0, 1.05, 60), _note(1.3, 1.35, 60), _note(1.6, 1.65, 60)]

    kept = _drop_isolated_notes(run, max_duration_s=0.15, neighbour_window_s=1.0)

    assert kept == run


def test_drop_isolated_notes_keeps_a_long_isolated_note() -> None:
    """Isolation alone is not suspicious -- only isolation *plus* brevity."""
    sustained = _note(30.0, 32.0, 77)

    kept = _drop_isolated_notes([sustained], max_duration_s=0.15, neighbour_window_s=1.0)

    assert kept == [sustained]


def test_clamp_sustain_truncates_a_runaway_note_but_leaves_short_notes_alone() -> None:
    runaway = _note(0.0, 126.0, 37)
    short = _note(1.0, 1.5, 60)

    clamped = _clamp_sustain([runaway, short], max_duration_s=4.0)

    assert clamped[0].end_s == pytest.approx(4.0)
    assert clamped[0].start_s == 0.0  # onset untouched
    assert clamped[1] == short


def test_drop_harmonic_ghosts_removes_an_octave_partial_of_a_louder_concurrent_note() -> None:
    fundamental = _note(10.0, 12.0, 48, velocity=95)
    octave_ghost = _note(10.02, 11.9, 60, velocity=80)  # 12 semitones above, near-identical span

    kept = _drop_harmonic_ghosts(
        [fundamental, octave_ghost],
        intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        onset_tolerance_s=GUITAR_GHOST_ONSET_TOLERANCE_S,
        overlap_fraction=GUITAR_GHOST_OVERLAP_FRACTION,
        velocity_slack=GUITAR_GHOST_VELOCITY_SLACK,
    )

    assert kept == [fundamental]


def test_drop_harmonic_ghosts_keeps_an_independent_note_at_a_harmonic_interval() -> None:
    """A real note an octave above another, played at a different time, must survive."""
    first = _note(0.0, 1.0, 48)
    second = _note(5.0, 6.0, 60)  # same interval, but not concurrent

    kept = _drop_harmonic_ghosts(
        [first, second],
        intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        onset_tolerance_s=GUITAR_GHOST_ONSET_TOLERANCE_S,
        overlap_fraction=GUITAR_GHOST_OVERLAP_FRACTION,
        velocity_slack=GUITAR_GHOST_VELOCITY_SLACK,
    )

    assert kept == [first, second]


def test_drop_harmonic_ghosts_keeps_a_louder_note_even_at_a_harmonic_interval() -> None:
    """A quieter note underneath a louder one above it is not a ghost of the
    louder note -- direction only runs from the lower pitch up."""
    lower_quiet = _note(0.0, 2.0, 48, velocity=40)
    upper_loud = _note(0.0, 2.0, 60, velocity=100)

    kept = _drop_harmonic_ghosts(
        [lower_quiet, upper_loud],
        intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        onset_tolerance_s=GUITAR_GHOST_ONSET_TOLERANCE_S,
        overlap_fraction=GUITAR_GHOST_OVERLAP_FRACTION,
        velocity_slack=GUITAR_GHOST_VELOCITY_SLACK,
    )

    assert kept == [lower_quiet, upper_loud]


def _write_geometric_harmonic_series_wav(
    path: Path,
    *,
    fundamental_hz: float,
    window_s: tuple[float, float],
    duration_s: float,
    decay_ratio: float = 0.6,
    max_order: int = 5,
    independent_order: int | None = None,
    independent_amplitude: float = 0.0,
    sample_rate: int = 22050,
) -> None:
    """Synthesize `max_order` harmonics of `fundamental_hz` at a clean
    geometric decay (`amplitude(order) = decay_ratio ** order`), sounding only
    during `window_s`, and write it to `path` as a float WAV.

    A perfectly geometric decay makes the spectral gate's log-linear fit
    reproduce the design amplitudes almost exactly, so the two synthetic
    cases below are unambiguous: one harmonic order can be swapped out for
    `independent_amplitude`, energy the parent's own series does not predict,
    to simulate a real note sounding at that harmonic interval instead of a
    ringing partial.
    """
    import numpy as np
    import soundfile as sf

    samples = np.zeros(int(duration_s * sample_rate), dtype=np.float64)
    start_sample = int(window_s[0] * sample_rate)
    end_sample = int(window_s[1] * sample_rate)
    t = np.arange(end_sample - start_sample) / sample_rate

    tone = np.zeros_like(t)
    for order in range(1, max_order + 1):
        amplitude = independent_amplitude if order == independent_order else decay_ratio**order
        tone += amplitude * np.sin(2 * np.pi * fundamental_hz * order * t)

    samples[start_sample:end_sample] = tone
    sf.write(str(path), samples.astype(np.float32), sample_rate, subtype="FLOAT")


def test_spectral_gate_keeps_a_real_octave_with_independent_energy_at_its_fundamental(tmp_path: Path) -> None:
    """A real octave played alongside the fundamental has its own strong
    energy at the octave frequency, far beyond what the fundamental's own
    (otherwise clean, decaying) harmonic series predicts there -- the
    heuristic's drop must be overridden and the octave kept."""
    parent = _note(0.5, 1.5, 48, velocity=90)
    ghost = _note(0.52, 1.45, 60, velocity=80)  # +12 semitones -> harmonic order 2

    source = tmp_path / "octave.wav"
    _write_geometric_harmonic_series_wav(
        source,
        fundamental_hz=_midi_to_hz(parent.pitch_midi),
        window_s=(parent.start_s, parent.end_s),
        duration_s=2.0,
        independent_order=2,
        independent_amplitude=2.0,  # far above the ~0.36 the decay curve predicts
    )
    spectral = _load_spectral_analysis(source)

    kept = _drop_harmonic_ghosts(
        [parent, ghost],
        intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        onset_tolerance_s=GUITAR_GHOST_ONSET_TOLERANCE_S,
        overlap_fraction=GUITAR_GHOST_OVERLAP_FRACTION,
        velocity_slack=GUITAR_GHOST_VELOCITY_SLACK,
        spectral_max_harmonic_order=GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
        spectral_freq_tolerance_semitones=GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
        spectral_independent_energy_ratio=GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
        spectral=spectral,
    )

    assert kept == [parent, ghost]


def test_spectral_gate_drops_a_pure_harmonic_partial_with_no_independent_energy(tmp_path: Path) -> None:
    """A ringing octave partial's amplitude sits exactly on the fundamental's
    own decay curve -- nothing at that frequency is unexplained, so the
    heuristic's drop must be confirmed, not overridden."""
    parent = _note(0.5, 1.5, 48, velocity=90)
    ghost = _note(0.52, 1.45, 60, velocity=80)  # +12 semitones -> harmonic order 2

    source = tmp_path / "partial.wav"
    _write_geometric_harmonic_series_wav(
        source,
        fundamental_hz=_midi_to_hz(parent.pitch_midi),
        window_s=(parent.start_s, parent.end_s),
        duration_s=2.0,
        # no independent_order override -- every harmonic, including order 2,
        # follows the same clean decay curve.
    )
    spectral = _load_spectral_analysis(source)

    kept = _drop_harmonic_ghosts(
        [parent, ghost],
        intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        onset_tolerance_s=GUITAR_GHOST_ONSET_TOLERANCE_S,
        overlap_fraction=GUITAR_GHOST_OVERLAP_FRACTION,
        velocity_slack=GUITAR_GHOST_VELOCITY_SLACK,
        spectral_max_harmonic_order=GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
        spectral_freq_tolerance_semitones=GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
        spectral_independent_energy_ratio=GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
        spectral=spectral,
    )

    assert kept == [parent]


def test_spectral_gate_never_widens_a_drop_the_heuristic_did_not_already_flag(tmp_path: Path) -> None:
    """The gate only ever retains a flagged note -- it must never cause a note
    the heuristic itself would have kept (not concurrent enough here) to be
    dropped, regardless of what the spectrum shows."""
    first = _note(0.0, 1.0, 48)
    second = _note(5.0, 6.0, 60)  # same interval, but not concurrent -- never flagged

    source = tmp_path / "unrelated.wav"
    _write_geometric_harmonic_series_wav(
        source,
        fundamental_hz=_midi_to_hz(first.pitch_midi),
        window_s=(first.start_s, first.end_s),
        duration_s=6.0,
    )
    spectral = _load_spectral_analysis(source)

    kept = _drop_harmonic_ghosts(
        [first, second],
        intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        onset_tolerance_s=GUITAR_GHOST_ONSET_TOLERANCE_S,
        overlap_fraction=GUITAR_GHOST_OVERLAP_FRACTION,
        velocity_slack=GUITAR_GHOST_VELOCITY_SLACK,
        spectral=spectral,
    )

    assert kept == [first, second]


def test_apply_cleanup_stages_loads_audio_lazily_only_when_a_ghost_stage_is_present(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """A target's cleanup pipeline that never includes `drop_harmonic_ghosts`
    (e.g. `bass-monophonic`) must never attempt to load or analyze audio,
    even when a `source` path is supplied."""
    from vgt import transcribe as transcribe_module

    def fail(*_args, **_kwargs):
        raise AssertionError("must not load audio for a cleanup pipeline with no ghost-drop stage")

    monkeypatch.setattr(transcribe_module, "_load_spectral_analysis", fail)

    spec = default_spec_for_target("bass", modes={"bass": "bass-monophonic"})
    notes = [_note(0.0, 1.0, 40), _note(0.5, 1.5, 41)]

    cleaned = _apply_cleanup_stages(notes, spec, source=tmp_path / "does-not-exist.wav")

    assert cleaned  # ran without touching the (non-existent) audio file


def test_cap_simultaneous_voices_truncates_the_quietest_active_voice_when_a_new_note_arrives() -> None:
    """The cap only ever retires an *already-sounding* voice to admit a new
    onset -- it never rejects the new note itself, even if the new note
    happens to be quieter than what it displaces."""
    quiet_holdover = _note(0.0, 5.0, 90, velocity=10)
    loud_chord = [_note(0.0, 5.0, 40 + i, velocity=90) for i in range(2)]
    new_arrival = _note(1.0, 4.0, 55, velocity=95)  # the trio is already full when this arrives

    capped = _cap_simultaneous_voices(
        [quiet_holdover, *loud_chord, new_arrival],
        max_voices=3,
        min_duration_after_cap_s=GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    )

    quiet_result = next(note for note in capped if note.pitch_midi == 90)
    assert quiet_result.end_s == pytest.approx(1.0)  # truncated at the new arrival's onset, not deleted
    assert {note.pitch_midi for note in capped if note.start_s == 0.0} == {90, 40, 41}
    assert 55 in {note.pitch_midi for note in capped}


def test_cap_simultaneous_voices_preserves_a_chord_within_the_limit() -> None:
    chord = [_note(0.0, 5.0, 40 + i, velocity=90) for i in range(6)]

    capped = _cap_simultaneous_voices(
        chord,
        max_voices=6,
        min_duration_after_cap_s=GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    )

    assert len(capped) == 6
    assert capped == chord


def test_cap_simultaneous_voices_retires_forward_not_only_at_the_new_notes_onset() -> None:
    """A voice retired early must stay retired for later overlap checks, not
    just get truncated once and then still count as active afterwards."""
    long_quiet = _note(0.0, 10.0, 40, velocity=10)
    fillers = [_note(1.0, 9.0, 50 + i, velocity=90) for i in range(3)]
    another_new_note = _note(2.0, 9.0, 70, velocity=90)

    capped = _cap_simultaneous_voices(
        [long_quiet, *fillers, another_new_note],
        max_voices=3,
        min_duration_after_cap_s=GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    )

    quiet_result = next(note for note in capped if note.pitch_midi == 40)
    assert quiet_result.end_s <= 1.0  # retired at the first filler's onset, not later


def test_force_monophony_resolves_exact_onset_ties_by_velocity_then_pitch() -> None:
    quieter = _note(0.0, 2.0, 48, velocity=90)
    lower_pitch = _note(0.0, 2.0, 52, velocity=100)
    higher_pitch = _note(0.0, 2.0, 60, velocity=100)

    cleaned = _force_monophony([quieter, higher_pitch, lower_pitch])

    assert cleaned == [lower_pitch]
    assert _max_polyphony(cleaned) <= 1


def test_force_monophony_truncates_a_note_that_contains_the_winner() -> None:
    held = _note(0.0, 4.0, 40, velocity=70)
    contained = _note(1.0, 2.0, 52, velocity=100)

    cleaned = _force_monophony([held, contained])

    assert cleaned == [_note(0.0, 1.0, 40, velocity=70), contained]
    assert _max_polyphony(cleaned) <= 1


def test_force_monophony_prefers_an_earlier_onset_when_velocity_ties() -> None:
    first = _note(0.0, 3.0, 60, velocity=100)
    later = _note(1.0, 2.0, 48, velocity=100)

    cleaned = _force_monophony([first, later])

    assert cleaned == [first]
    assert _max_polyphony(cleaned) <= 1


def test_force_monophony_resolves_a_chain_of_overlaps_by_velocity_then_onset() -> None:
    first = _note(0.0, 4.0, 40, velocity=80)
    second = _note(1.0, 4.0, 45, velocity=90)
    third = _note(2.0, 4.0, 50, velocity=100)

    cleaned = _force_monophony([first, second, third])

    assert cleaned == [
        _note(0.0, 1.0, 40, velocity=80),
        _note(1.0, 2.0, 45, velocity=90),
        third,
    ]
    assert _max_polyphony(cleaned) == 1


def test_force_monophony_leaves_empty_input_empty() -> None:
    cleaned = _force_monophony([])

    assert cleaned == []
    assert _max_polyphony(cleaned) <= 1


def _max_polyphony(notes: list[ParsedNote]) -> int:
    edges = [(note.start_s, 1) for note in notes] + [(note.end_s, -1) for note in notes]
    edges.sort()
    voices = peak = 0
    for _time, delta in edges:
        voices += delta
        peak = max(peak, voices)
    return peak


def test_apply_guitar_cleanup_runs_every_stage_in_order() -> None:
    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=120.0)
    runaway_fundamental = _note(0.0, 999.0, 40, velocity=95)
    ghost = _note(0.0, 999.0, 52, velocity=80)  # octave above, would ghost off the fundamental
    blip = _note(500.0, 500.04, 77)  # short, nothing else at pitch 77
    # 90+i, not 60+i: must avoid landing on a harmonic interval above 40 or 52
    # (e.g. 40+24=64), or the ghost-drop step would remove one of these too.
    extra_voices = [_note(0.5, 3.0, 90 + i, velocity=90) for i in range(6)]  # pushes over 6 voices

    cleaned = _apply_cleanup_stages([runaway_fundamental, ghost, blip, *extra_voices], spec)

    sustain_clamp_s = _cleanup_params(spec, "clamp_sustain")["max_duration_s"]
    assert all(note.end_s - note.start_s <= sustain_clamp_s + 1e-9 for note in cleaned)
    assert 52 not in {note.pitch_midi for note in cleaned}  # ghost dropped
    assert 77 not in {note.pitch_midi for note in cleaned}  # isolated blip dropped
    assert _max_polyphony(cleaned) <= GUITAR_MAX_SIMULTANEOUS_VOICES


def test_apply_guitar_cleanup_merges_fragments_before_clamping_sustain() -> None:
    """The ordering finding this pipeline exists to encode.

    Two fragments of 3.5 s each are individually under the 4 s clamp, so
    clamping first leaves both untouched and a later merge produces a 7 s
    note -- past the clamp that already ran. Merging first yields one 7 s
    note that the clamp then cuts to 4 s.

    This is the assertion that actually discriminates: reordering the
    pipeline so the merge runs last makes it fail (verified by patching the
    order and re-running), whereas a polyphony-only assertion can pass under
    either order because the voice cap may simply drop the offending pitch
    outright.
    """
    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=120.0)
    fragments = [_note(0.0, 3.5, 60), _note(3.5, 7.0, 60)]  # zero-width split, each under the clamp

    cleaned = _apply_cleanup_stages(fragments, spec)

    assert len(cleaned) == 1
    assert cleaned[0].end_s - cleaned[0].start_s == pytest.approx(_cleanup_params(spec, "clamp_sustain")["max_duration_s"])
    assert _max_polyphony(cleaned) <= GUITAR_MAX_SIMULTANEOUS_VOICES


def test_transcribe_applies_guitar_cleanup_and_rewrites_both_artifacts(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """End-to-end: a real (fake-subprocess) basic-pitch run whose raw output
    contains a runaway drone must come out of `transcribe()` clamped, with
    both the CSV and MIDI reflecting the cleaned notes."""
    import subprocess
    import wave
    from types import SimpleNamespace

    source = tmp_path / "guitar.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8000)  # 1s of silence -- real audio the spectral gate can load
    destination = tmp_path / "out"
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        # The spectral gate's lazy `import librosa` pulls in scipy/numpy
        # machinery that itself shells out (e.g. numpy.testing probing `lscpu`
        # for SVE support) with a bare string argv -- only intercept our own
        # basic-pitch invocation and pass anything else through to the real
        # `subprocess.run`.
        if not isinstance(argv, list) or str(destination) not in argv:
            return real_run(argv, **kwargs)
        outdir = Path(argv[argv.index(str(destination))])
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "guitar_basic_pitch.mid").write_bytes(
            b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big")
            + (1).to_bytes(2, "big") + (480).to_bytes(2, "big")
            + b"MTrk" + (4).to_bytes(4, "big") + bytes([0x00, 0xFF, 0x2F, 0x00])
        )
        (outdir / "guitar_basic_pitch.csv").write_text(
            "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n"
            "0.0,60.0,40,90\n"  # runaway: far longer than the 2-bar clamp
            "0.5,1.5,60,70\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    spec = default_spec_for_target("guitar", modes={"guitar": "guitar-acoustic"}, midi_tempo=120.0)
    result = BasicPitchTranscriber().transcribe(source, destination, spec)

    sustain_clamp_s = _cleanup_params(spec, "clamp_sustain")["max_duration_s"]
    assert result.last_note_s <= sustain_clamp_s + 1e-6
    csv_text = result.notes_path.read_text(encoding="utf-8")
    assert "60.0" not in csv_text  # the raw 60s end time must not survive
    midi_bytes = result.midi_path.read_bytes()
    assert midi_bytes[:4] == b"MThd"


def _tone_wav(path: Path, frequency: float, duration: float = 1.2, sample_rate: int = 22050) -> Path:
    """A pure sine at `frequency`, written with soundfile (a vgt dependency)."""
    import math

    import soundfile

    samples = [
        0.5 * math.sin(2 * math.pi * frequency * index / sample_rate)
        for index in range(int(sample_rate * duration))
    ]
    soundfile.write(str(path), samples, sample_rate)
    return path


def test_pyin_transcriber_writes_a_valid_midi_csv_pair_for_a_bass_tone(tmp_path: Path) -> None:
    """The real pyin backend end to end, on audio the test synthesizes itself.

    98 Hz is an open bass G string (G2, MIDI 43). This exercises the in-process
    path -- no subprocess, no `uvx`, no model download -- so it stays in the
    normal offline suite rather than needing a marker.
    """
    source = _tone_wav(tmp_path / "bass.wav", 98.0)
    spec = default_spec_for_target("bass", midi_tempo=120.0, time_signature="4/4")

    result = PyinTranscriber().transcribe(source, tmp_path / "out", spec)

    assert result.note_count >= 1
    assert result.pitch_range_midi == (43, 43)
    assert result.max_simultaneous_voices == 1
    assert result.midi_path.read_bytes()[:4] == b"MThd"
    header = result.notes_path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend"


def test_pyin_transcriber_applies_the_profiles_sustain_clamp(tmp_path: Path) -> None:
    """A tone held past the clamp must be shortened, not emitted whole.

    This is the drone failure mode the Basic Pitch bass profile had no defence
    against: a tracker can also hold a note through a rest.
    """
    source = _tone_wav(tmp_path / "bass.wav", 98.0, duration=6.0)
    # 2 bars at 240 BPM 4/4 is 2.0s, comfortably inside the 6s tone.
    spec = default_spec_for_target("bass", midi_tempo=240.0, time_signature="4/4")
    clamp_s = _cleanup_params(spec, "clamp_sustain")["max_duration_s"]

    result = PyinTranscriber().transcribe(source, tmp_path / "out", spec)

    assert clamp_s == pytest.approx(2.0)
    assert result.max_note_duration_s <= clamp_s + 1e-6


def test_pyin_transcriber_rejects_a_spec_from_another_backend(tmp_path: Path) -> None:
    source = _tone_wav(tmp_path / "bass.wav", 98.0, duration=0.2)
    guitar = default_spec_for_target("guitar")

    with pytest.raises(TranscriptionError, match="requires a PyinSpec"):
        PyinTranscriber().transcribe(source, tmp_path / "out", guitar)


def test_pyin_transcriber_reports_unreadable_audio_as_a_transcription_error(tmp_path: Path) -> None:
    """Failure must be a `TranscriptionError`, so one bad stem degrades that
    target only instead of aborting the whole analysis run."""
    source = tmp_path / "bass.wav"
    source.write_bytes(b"not audio at all")
    spec = default_spec_for_target("bass", midi_tempo=120.0)

    with pytest.raises(TranscriptionError, match="pyin failed"):
        PyinTranscriber().transcribe(source, tmp_path / "out", spec)


def test_pyin_detection_identity_excludes_cleanup_so_retuning_it_reuses_the_track(tmp_path: Path) -> None:
    """Two bass variants differing only in cleanup must share one F0 track.

    The pitch track is the expensive part of this backend, so a `pyin` variant
    has to join the same two-level cache Basic Pitch variants use rather than
    re-running the tracker per cleanup recipe.
    """
    from dataclasses import replace as dataclass_replace

    from vgt.transcription_variants import cleanup_hash, detection_hash

    spec = default_spec_for_target("bass", midi_tempo=120.0, time_signature="4/4")
    lighter = dataclass_replace(spec, cleanup=spec.cleanup[:1])

    assert detection_hash("bass", "input-hash", spec) == detection_hash("bass", "input-hash", lighter)
    assert cleanup_hash("raw-hash", "input-hash", spec) != cleanup_hash("raw-hash", "input-hash", lighter)


def test_pyin_and_basic_pitch_bass_variants_never_share_a_detection_entry() -> None:
    from vgt.transcription_variants import detection_hash

    pyin = default_spec_for_target("bass", midi_tempo=120.0)
    basic_pitch = default_spec_for_target("bass", modes={"bass": "bass-basic-pitch"}, midi_tempo=120.0)

    assert detection_hash("bass", "input-hash", pyin) != detection_hash("bass", "input-hash", basic_pitch)


def test_pyin_detection_identity_tracks_the_algorithm_version() -> None:
    """An algorithm change must invalidate a cached track, since librosa's own
    version is deliberately not part of the identity."""
    from dataclasses import replace as dataclass_replace

    from vgt.transcription_variants import detection_hash

    spec = default_spec_for_target("bass", midi_tempo=120.0)
    next_version = dataclass_replace(spec, algorithm_version=spec.algorithm_version + 1)

    assert detection_hash("bass", "input-hash", spec) != detection_hash("bass", "input-hash", next_version)
    assert spec_hash(spec) != spec_hash(next_version)
