from pathlib import Path
import collections
import hashlib
import json
import struct

import pytest

from vgt.transcribe import (
    BasicPitchSpec,
    BasicPitchTranscriber,
    CleanupStage,
    DrumScriptSpec,
    DrumScriptTranscriber,
    GUITAR_GHOST_ONSET_TOLERANCE_S,
    GUITAR_GHOST_OVERLAP_FRACTION,
    GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
    GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
    GUITAR_GHOST_VELOCITY_SLACK,
    GUITAR_MAX_SIMULTANEOUS_VOICES,
    GUITAR_HARMONIC_GHOST_INTERVALS,
    GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    VALID_TARGETS,
    FakeTranscriber,
    ParsedNote,
    TargetTranscriberRouter,
    TranscriptionError,
    _apply_cleanup_stages,
    _bar_duration_seconds,
    _cap_simultaneous_voices,
    _clamp_sustain,
    _drop_harmonic_ghosts,
    _drop_isolated_notes,
    _force_monophony,
    _merge_fragments,
    default_spec_for_target,
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
    vocals = default_spec_for_target("vocals")
    piano = default_spec_for_target("piano")

    assert (guitar.minimum_frequency_hz, guitar.maximum_frequency_hz) == (70.0, 1400.0)
    assert (bass.minimum_frequency_hz, bass.maximum_frequency_hz) == (30.0, 400.0)
    assert (vocals.minimum_frequency_hz, vocals.maximum_frequency_hz) == (70.0, 1200.0)
    # Polyphonic/unpredictable targets get Basic Pitch's own full-range defaults.
    assert (piano.minimum_frequency_hz, piano.maximum_frequency_hz) == (None, None)


def test_default_spec_leaves_unrecognised_and_unset_guitar_at_the_generic_defaults() -> None:
    """Stored modes from a retired registry entry safely use the default."""
    unset = default_spec_for_target("guitar")
    electric = default_spec_for_target("guitar", modes={"guitar": "electric"})

    for spec in (unset, electric):
        assert (spec.minimum_frequency_hz, spec.maximum_frequency_hz) == (70.0, 1400.0)
        assert spec.onset_threshold == 0.5
        assert spec.frame_threshold == 0.3
        assert spec.minimum_note_length_ms == 60.0
        assert spec.melodia_trick is True
        assert spec.cleanup == ()


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
        "spectral_max_harmonic_order": GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
        "spectral_freq_tolerance_semitones": GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
        "spectral_independent_energy_ratio": GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    }
    # Two bars at 120 BPM 4/4: 2 * 4 beats * 60/120 = 4.0s.
    assert _cleanup_params(spec, "clamp_sustain")["max_duration_s"] == pytest.approx(4.0)


def test_default_spec_mode_override_is_target_local() -> None:
    bass = default_spec_for_target("bass", modes={"guitar": "guitar-acoustic"})

    assert (bass.minimum_frequency_hz, bass.maximum_frequency_hz) == (30.0, 400.0)
    assert bass.cleanup == ()


def test_bass_monophonic_profile_is_explicit_and_leaves_bass_default_unchanged() -> None:
    default = default_spec_for_target("bass")
    monophonic = default_spec_for_target("bass", modes={"bass": "bass-monophonic"})

    assert default.cleanup == ()
    assert _cleanup_names(monophonic) == ["force_monophony"]


def test_default_spec_ignores_a_stored_profile_for_another_target() -> None:
    """A stale or malformed sidecar mode must retain the target default."""
    bass = default_spec_for_target("bass", modes={"bass": "guitar-acoustic"})

    assert (bass.minimum_frequency_hz, bass.maximum_frequency_hz) == (30.0, 400.0)
    assert bass.cleanup == ()


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
    bass = default_spec_for_target("bass")

    assert guitar.backend == bass.backend == "basic-pitch"
    assert guitar.minimum_note_length_ms == bass.minimum_note_length_ms == 60.0
    assert guitar.melodia_trick is True
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
    dict so a regression here (e.g. someone dropping the shim) is caught."""
    for target in ("guitar", "bass", "vocals", "piano", "strings", "instrumental", "backing", "original"):
        spec = default_spec_for_target(target)
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


def test_router_threads_modes_and_time_signature_through_to_the_spec() -> None:
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber())

    spec = router.spec_for_target("guitar", midi_tempo=120.0, modes={"guitar": "guitar-acoustic"}, time_signature="3/4")

    assert "drop_harmonic_ghosts" in _cleanup_names(spec)
    assert _cleanup_params(spec, "clamp_sustain")["max_duration_s"] == pytest.approx(3.0)


def test_production_router_sends_drums_to_drumscript_and_everything_else_to_basic_pitch() -> None:
    router = production_transcriber_router()

    for target in VALID_TARGETS:
        if target == "drums":
            assert router.for_target(target).name == "drumscript"
            assert isinstance(router.for_target(target), DrumScriptTranscriber)
        else:
            assert router.for_target(target).name == "basic-pitch"
            assert isinstance(router.for_target(target), BasicPitchTranscriber)
    assert isinstance(router.spec_for_target("drums", midi_tempo=120.0), DrumScriptSpec)
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
