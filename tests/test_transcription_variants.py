from pathlib import Path
from typing import Any, Callable
import hashlib

import pytest

from vgt.transcribe import (
    FakeTranscriber,
    RawDetectionResult,
    TranscriptionError,
    TranscriptionSpec,
    default_spec_for_target,
)
from vgt.transcription_variants import (
    VariantRequest,
    cleanup_hash,
    detection_hash,
    garbage_collect_raw_cache,
    reconcile_artifact_layout,
    reconcile_variants,
)


def _write_source(tmp_path: Path, name: str = "guitar.wav", content: bytes = b"fake-audio-bytes") -> Path:
    """A real, librosa-loadable WAV (unlike test_transcribe.py's raw-bytes
    stub source): several built-in guitar-acoustic profiles include
    `drop_harmonic_ghosts`, whose spectral confirmation gate actually reads
    the source file. Its samples are derived from `content` so distinct
    `content` still reliably produces distinct file bytes for the
    content-addressing tests below."""
    import numpy as np
    import soundfile as sf

    seed = int.from_bytes(hashlib.sha256(content).digest()[:4], "big")
    rng = np.random.default_rng(seed)
    samples = (rng.standard_normal(66150) * 0.01).astype(np.float32)  # 3s @ 22050 Hz, quiet noise
    path = tmp_path / name
    sf.write(str(path), samples, 22050, subtype="FLOAT")
    return path


def _input_hash(source: Path) -> str:
    return hashlib.sha256(source.read_bytes()).hexdigest()


class _CountingFake(FakeTranscriber):
    """Wraps `FakeTranscriber` to record every `detect_raw`/`transcribe` call,
    and optionally fail for specs matching a predicate -- lets a test isolate
    one detection group's failure from its siblings without touching real
    Basic Pitch."""

    def __init__(self, *, fail_when: Callable[[TranscriptionSpec], bool] | None = None) -> None:
        self.raw_calls: list[TranscriptionSpec] = []
        self.transcribe_calls: list[TranscriptionSpec] = []
        self._fail_when = fail_when or (lambda _spec: False)

    def detect_raw(self, source, destination_dir, spec, progress=None) -> RawDetectionResult:
        self.raw_calls.append(spec)
        if self._fail_when(spec):
            raise TranscriptionError("synthetic detection failure")
        return super().detect_raw(source, destination_dir, spec, progress)

    def transcribe(self, source, destination_dir, spec, progress=None):
        self.transcribe_calls.append(spec)
        if self._fail_when(spec):
            raise TranscriptionError("synthetic drumscript failure")
        return super().transcribe(source, destination_dir, spec, progress)


def _variant(variant_id: str, label: str, profile: str, *, target: str = "guitar", **spec_kwargs: Any) -> VariantRequest:
    spec = default_spec_for_target(target, backend="fake", modes={target: profile}, **spec_kwargs)
    return VariantRequest(
        variant_id=variant_id,
        label=label,
        requested_profile=profile,
        effective_profile=profile,
        profile_definition_hash=None,
        spec=spec,
        resolved_settings={"detection": {}, "cleanup": []},
    )


def test_detail_and_clean_share_one_detection_hash_and_one_backend_invocation(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    clean = _variant("v-clean", "clean", "guitar-acoustic-clean")
    transcriber = _CountingFake()

    outcome = reconcile_variants(
        target="guitar",
        requests=[detail, clean],
        transcriber=transcriber,
        source=source,
        input_hash=_input_hash(source),
        namespace_dir=tmp_path / "ns",
    )

    assert outcome.backend_invocations == 1
    assert len(transcriber.raw_calls) == 1
    assert outcome.variants["v-detail"]["status"] == "transcribed"
    assert outcome.variants["v-clean"]["status"] == "transcribed"
    assert outcome.variants["v-detail"]["detection_hash"] == outcome.variants["v-clean"]["detection_hash"]
    assert len(outcome.detection_cache) == 1


def test_strict_chords_needs_its_own_detection_group(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    strict = _variant("v-strict", "strict", "guitar-acoustic-strict-chords")
    transcriber = _CountingFake()

    outcome = reconcile_variants(
        target="guitar",
        requests=[detail, strict],
        transcriber=transcriber,
        source=source,
        input_hash=_input_hash(source),
        namespace_dir=tmp_path / "ns",
    )

    assert outcome.backend_invocations == 2
    assert len(outcome.detection_cache) == 2
    assert outcome.variants["v-detail"]["detection_hash"] != outcome.variants["v-strict"]["detection_hash"]


def test_derived_artifacts_are_persisted_at_variant_scoped_paths(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    namespace_dir = tmp_path / "ns"
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")

    outcome = reconcile_variants(
        target="guitar",
        requests=[detail],
        transcriber=_CountingFake(),
        source=source,
        input_hash=_input_hash(source),
        namespace_dir=namespace_dir,
    )

    record = outcome.variants["v-detail"]
    assert record["midi_file"] == "transcription/guitar/v-detail.mid"
    assert record["notes_file"] == "transcription/guitar/v-detail.csv"
    assert (namespace_dir / record["midi_file"]).is_file()
    assert (namespace_dir / record["notes_file"]).is_file()
    cache_entry = next(iter(outcome.detection_cache.values()))
    assert (namespace_dir / cache_entry["raw_midi_file"]).is_file()
    assert (namespace_dir / cache_entry["raw_notes_file"]).is_file()


def test_cleanup_only_change_recomputes_only_the_dependent_variant(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    namespace_dir = tmp_path / "ns"
    transcriber = _CountingFake()
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    clean = _variant("v-clean", "clean", "guitar-acoustic-clean")

    first = reconcile_variants(
        target="guitar", requests=[detail, clean], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
    )
    assert transcriber.raw_calls == [detail.spec]  # only the representative spec of the shared group ran

    # Retune clean's cleanup (a project-profile-style override) without touching detection.
    from dataclasses import replace

    from vgt.transcribe import CleanupStage

    retuned_cleanup = tuple(
        CleanupStage("cap_simultaneous_voices", {"max_voices": 2, "min_duration_after_cap_s": 0.04})
        if stage.name == "cap_simultaneous_voices" else stage
        for stage in clean.spec.cleanup
    )
    retuned_clean = replace(clean, spec=replace(clean.spec, cleanup=retuned_cleanup))

    second = reconcile_variants(
        target="guitar", requests=[detail, retuned_clean], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
        existing_variants=first.variants, detection_cache=first.detection_cache,
    )

    assert second.backend_invocations == 0  # raw cache still valid; no new Basic Pitch call
    assert second.variants["v-detail"] is first.variants["v-detail"]  # untouched variant reused verbatim
    assert second.variants["v-clean"]["cleanup_hash"] != first.variants["v-clean"]["cleanup_hash"]
    assert second.variants["v-clean"]["transcribed_at"] != first.variants["v-clean"]["transcribed_at"]


def test_detection_setting_change_invalidates_its_group_and_dependents(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    namespace_dir = tmp_path / "ns"
    transcriber = _CountingFake()
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")

    first = reconcile_variants(
        target="guitar", requests=[detail], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
    )

    from dataclasses import replace

    retuned = replace(detail, spec=replace(detail.spec, onset_threshold=0.9))
    second = reconcile_variants(
        target="guitar", requests=[retuned], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
        existing_variants=first.variants, detection_cache=first.detection_cache,
    )

    assert second.backend_invocations == 1
    assert len(transcriber.raw_calls) == 2
    assert second.variants["v-detail"]["detection_hash"] != first.variants["v-detail"]["detection_hash"]
    # The stale group's cache entry survives until garbage collection runs.
    assert len(second.detection_cache) == 2


def test_source_change_invalidates_raw_group_and_derived_variants(tmp_path: Path) -> None:
    namespace_dir = tmp_path / "ns"
    transcriber = _CountingFake()
    source_a = _write_source(tmp_path, "a.wav", b"content-a")
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")

    first = reconcile_variants(
        target="guitar", requests=[detail], transcriber=transcriber,
        source=source_a, input_hash=_input_hash(source_a), namespace_dir=namespace_dir,
    )

    source_b = _write_source(tmp_path, "b.wav", b"content-b")
    second = reconcile_variants(
        target="guitar", requests=[detail], transcriber=transcriber,
        source=source_b, input_hash=_input_hash(source_b), namespace_dir=namespace_dir,
        existing_variants=first.variants, detection_cache=first.detection_cache,
    )

    assert second.backend_invocations == 1
    assert second.variants["v-detail"]["input_hash"] != first.variants["v-detail"]["input_hash"]
    assert second.variants["v-detail"]["detection_hash"] != first.variants["v-detail"]["detection_hash"]


def test_missing_source_produces_a_retained_state_per_variant_with_no_invocation(tmp_path: Path) -> None:
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    clean = _variant("v-clean", "clean", "guitar-acoustic-clean")
    transcriber = _CountingFake()

    outcome = reconcile_variants(
        target="guitar", requests=[detail, clean], transcriber=transcriber,
        source=None, input_hash=None, namespace_dir=tmp_path / "ns",
    )

    assert outcome.backend_invocations == 0
    assert transcriber.raw_calls == []
    assert outcome.variants["v-detail"]["status"] == "skipped-missing-source"
    assert outcome.variants["v-clean"]["status"] == "skipped-missing-source"


def test_missing_source_recovers_once_the_stem_arrives(tmp_path: Path) -> None:
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    transcriber = _CountingFake()

    missing = reconcile_variants(
        target="guitar", requests=[detail], transcriber=transcriber,
        source=None, input_hash=None, namespace_dir=tmp_path / "ns",
    )
    assert missing.variants["v-detail"]["status"] == "skipped-missing-source"

    source = _write_source(tmp_path)
    recovered = reconcile_variants(
        target="guitar", requests=[detail], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
        existing_variants=missing.variants,
    )
    assert recovered.variants["v-detail"]["status"] == "transcribed"
    assert recovered.backend_invocations == 1


def test_one_detection_groups_failure_does_not_affect_a_sibling_group(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    strict = _variant("v-strict", "strict", "guitar-acoustic-strict-chords")
    transcriber = _CountingFake(fail_when=lambda spec: getattr(spec, "onset_threshold", None) == strict.spec.onset_threshold)

    outcome = reconcile_variants(
        target="guitar", requests=[detail, strict], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )

    assert outcome.variants["v-detail"]["status"] == "transcribed"
    assert outcome.variants["v-strict"]["status"] == "error"
    assert outcome.variants["v-strict"]["error"]
    # The failed group's raw detection never succeeded, so nothing was cached for it.
    assert outcome.variants["v-detail"]["detection_hash"] in outcome.detection_cache
    assert outcome.variants["v-strict"]["detection_hash"] not in outcome.detection_cache


def test_per_variant_derive_failure_isolates_from_its_group_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_source(tmp_path)
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    clean = _variant("v-clean", "clean", "guitar-acoustic-clean")

    import vgt.transcription_variants as tv

    real_derive = tv.derive_variant_artifacts

    def flaky_derive(raw_notes, spec, **kwargs):
        if spec is clean.spec:
            raise TranscriptionError("synthetic cleanup failure")
        return real_derive(raw_notes, spec, **kwargs)

    monkeypatch.setattr(tv, "derive_variant_artifacts", flaky_derive)

    outcome = reconcile_variants(
        target="guitar", requests=[detail, clean], transcriber=_CountingFake(),
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )

    assert outcome.variants["v-detail"]["status"] == "transcribed"
    assert outcome.variants["v-clean"]["status"] == "error"
    assert outcome.backend_invocations == 1  # the shared raw detection still only ran once


def test_persist_callbacks_fire_immediately_per_variant_and_cache_entry(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    clean = _variant("v-clean", "clean", "guitar-acoustic-clean")
    persisted_variants: list[str] = []
    persisted_cache: list[str] = []

    reconcile_variants(
        target="guitar", requests=[detail, clean], transcriber=_CountingFake(),
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
        persist_variant=lambda variant_id, _record: persisted_variants.append(variant_id),
        persist_detection_cache_entry=lambda dh, _entry: persisted_cache.append(dh),
    )

    assert set(persisted_variants) == {"v-detail", "v-clean"}
    assert len(persisted_cache) == 1


def test_drumscript_variant_runs_full_transcribe_with_no_grouping(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    spec = default_spec_for_target("drums", backend="drumscript")
    request = VariantRequest(
        variant_id="v-default", label="default", requested_profile="default",
        effective_profile="default", profile_definition_hash=None, spec=spec,
        resolved_settings={"detection": {}, "cleanup": []},
    )
    transcriber = _CountingFake()

    outcome = reconcile_variants(
        target="drums", requests=[request], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )

    assert outcome.backend_invocations == 1
    assert len(transcriber.transcribe_calls) == 1
    assert outcome.variants["v-default"]["status"] == "transcribed"
    assert outcome.variants["v-default"]["events_file"] == "transcription/drums/v-default.json"
    assert outcome.detection_cache == {}  # drums never write into the raw detection cache
    # DrumScript's tempo is detected from its own output, not an input setting.
    assert outcome.variants["v-default"]["midi_tempo"] == 120.0


def test_drumscript_variant_is_cached_and_not_rerun_when_unchanged(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    spec = default_spec_for_target("drums", backend="drumscript")
    request = VariantRequest(
        variant_id="v-default", label="default", requested_profile="default",
        effective_profile="default", profile_definition_hash=None, spec=spec,
        resolved_settings={"detection": {}, "cleanup": []},
    )
    transcriber = _CountingFake()

    first = reconcile_variants(
        target="drums", requests=[request], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )
    second = reconcile_variants(
        target="drums", requests=[request], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
        existing_variants=first.variants,
    )

    assert len(transcriber.transcribe_calls) == 1
    assert second.variants["v-default"] is first.variants["v-default"]


def test_drumscript_clean_variant_gets_its_own_cache_identity_and_channel_10_midi(tmp_path: Path) -> None:
    """Issue #177: selecting `drums-clean` must not touch the `default`
    drums variant's artifacts/identity, must produce its own distinct
    settings_hash-keyed cache entry, and must guarantee GM percussion
    channel 10 output even though it's regenerated (not copied) MIDI."""
    from vgt.transcribe import _midi_has_non_percussion_notes

    source = _write_source(tmp_path)
    default_spec = default_spec_for_target("drums", backend="drumscript")
    clean_spec = default_spec_for_target("drums", backend="drumscript", modes={"drums": "drums-clean"})
    default_request = VariantRequest(
        variant_id="v-default", label="default", requested_profile="default",
        effective_profile="default", profile_definition_hash=None, spec=default_spec,
        resolved_settings={"cleanup_profile": "default"},
    )
    clean_request = VariantRequest(
        variant_id="v-clean", label="clean", requested_profile="drums-clean",
        effective_profile="drums-clean", profile_definition_hash=None, spec=clean_spec,
        resolved_settings={"cleanup_profile": "drums-clean"},
    )
    transcriber = _CountingFake()

    outcome = reconcile_variants(
        target="drums", requests=[default_request, clean_request], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )

    default_variant = outcome.variants["v-default"]
    clean_variant = outcome.variants["v-clean"]
    assert default_variant["settings_hash"] != clean_variant["settings_hash"]
    assert clean_variant["status"] == "transcribed"

    # `midi_file` is a namespace-relative path (e.g. "drums/v-clean.mid").
    midi_path = tmp_path / "ns" / clean_variant["midi_file"]
    assert not _midi_has_non_percussion_notes(midi_path.read_bytes())


def test_drumscript_clean_variant_reconciliation_only_invalidates_its_own_variant_when_retuned(tmp_path: Path) -> None:
    from dataclasses import replace

    source = _write_source(tmp_path)
    clean_spec = default_spec_for_target("drums", backend="drumscript", modes={"drums": "drums-clean"})
    default_spec = default_spec_for_target("drums", backend="drumscript")
    default_request = VariantRequest(
        variant_id="v-default", label="default", requested_profile="default",
        effective_profile="default", profile_definition_hash=None, spec=default_spec,
        resolved_settings={"cleanup_profile": "default"},
    )
    clean_request = VariantRequest(
        variant_id="v-clean", label="clean", requested_profile="drums-clean",
        effective_profile="drums-clean", profile_definition_hash=None, spec=clean_spec,
        resolved_settings={"cleanup_profile": "drums-clean"},
    )
    transcriber = _CountingFake()

    first = reconcile_variants(
        target="drums", requests=[default_request, clean_request], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )

    # Retune only the clean spec's identity (simulating a later change to the
    # `drums-clean` recipe); the default request/spec is untouched.
    retuned_clean_spec = replace(clean_spec, cleanup=(replace(clean_spec.cleanup[0], name="drums-clean-retuned"),))
    retuned_clean_request = replace(clean_request, spec=retuned_clean_spec)

    second = reconcile_variants(
        target="drums", requests=[default_request, retuned_clean_request], transcriber=transcriber,
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
        existing_variants=first.variants,
    )

    assert second.variants["v-default"] is first.variants["v-default"]  # untouched
    assert second.variants["v-clean"] is not first.variants["v-clean"]  # rerun
    assert len(transcriber.transcribe_calls) == 3  # default once, clean twice (initial + retuned)


def test_spectrogram_is_computed_once_per_run_across_variants_needing_ghost_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vgt.transcribe as transcribe_module

    calls: list[tuple[int, int]] = []
    real_load = transcribe_module._load_spectral_analysis

    def counting_load(source, *, n_fft, hop_length):
        calls.append((n_fft, hop_length))
        return real_load(source, n_fft=n_fft, hop_length=hop_length)

    monkeypatch.setattr(transcribe_module, "_load_spectral_analysis", counting_load)

    import soundfile as sf
    import numpy as np

    source = tmp_path / "guitar.wav"
    sf.write(str(source), np.zeros(4410, dtype=np.float32), 22050, subtype="FLOAT")

    clean_a = _variant("v-clean-a", "clean a", "guitar-acoustic-clean")
    # A second variant with a distinct id but identical spec: both land in
    # the same detection group and both run `drop_harmonic_ghosts`.
    clean_b = VariantRequest(
        variant_id="v-clean-b", label="clean b", requested_profile="guitar-acoustic-clean",
        effective_profile="guitar-acoustic-clean", profile_definition_hash=None,
        spec=clean_a.spec, resolved_settings={"detection": {}, "cleanup": []},
    )

    outcome = reconcile_variants(
        target="guitar", requests=[clean_a, clean_b], transcriber=_CountingFake(),
        source=source, input_hash=_input_hash(source), namespace_dir=tmp_path / "ns",
    )

    assert outcome.variants["v-clean-a"]["status"] == "transcribed"
    assert outcome.variants["v-clean-b"]["status"] == "transcribed"
    assert len(calls) == 1


def test_gc_removes_only_unreferenced_raw_groups(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    namespace_dir = tmp_path / "ns"
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    strict = _variant("v-strict", "strict", "guitar-acoustic-strict-chords")

    outcome = reconcile_variants(
        target="guitar", requests=[detail, strict], transcriber=_CountingFake(),
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
    )
    assert len(outcome.detection_cache) == 2

    # Only "detail" is still a live variant; "strict" was discarded elsewhere.
    targets = {"guitar": {"variants": {"v-detail": outcome.variants["v-detail"]}}}
    kept, removed = garbage_collect_raw_cache(
        namespace_dir=namespace_dir, detection_cache=outcome.detection_cache, targets=targets
    )

    assert list(kept) == [outcome.variants["v-detail"]["detection_hash"]]
    assert removed == [outcome.variants["v-strict"]["detection_hash"]]
    kept_entry = outcome.detection_cache[outcome.variants["v-detail"]["detection_hash"]]
    assert (namespace_dir / kept_entry["raw_midi_file"]).is_file()
    removed_entry = outcome.detection_cache[outcome.variants["v-strict"]["detection_hash"]]
    assert not (namespace_dir / removed_entry["raw_midi_file"]).is_file()
    assert not (namespace_dir / removed_entry["raw_notes_file"]).is_file()


def test_gc_keeps_a_raw_group_shared_by_two_live_variants(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    namespace_dir = tmp_path / "ns"
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    clean = _variant("v-clean", "clean", "guitar-acoustic-clean")

    outcome = reconcile_variants(
        target="guitar", requests=[detail, clean], transcriber=_CountingFake(),
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
    )
    assert len(outcome.detection_cache) == 1
    dh = next(iter(outcome.detection_cache))

    targets = {"guitar": {"variants": outcome.variants}}
    kept, removed = garbage_collect_raw_cache(
        namespace_dir=namespace_dir, detection_cache=outcome.detection_cache, targets=targets
    )

    assert removed == []
    assert dh in kept
    entry = outcome.detection_cache[dh]
    assert (namespace_dir / entry["raw_midi_file"]).is_file()
    assert (namespace_dir / entry["raw_notes_file"]).is_file()


def test_gc_never_deletes_a_path_recorded_outside_its_own_cache_namespace(tmp_path: Path) -> None:
    namespace_dir = tmp_path / "ns"
    namespace_dir.mkdir(parents=True)
    outside = tmp_path / "outside.mid"
    outside.write_bytes(b"not-cache-owned")

    detection_cache = {
        "deadbeef": {
            "target": "guitar",
            "input_hash": "x",
            "raw_midi_file": "../outside.mid",
            "raw_notes_file": "transcription/cache/basic-pitch/deadbeef/raw.csv",
            "raw_notes_hash": "y",
            "created_at": "2026-01-01T00:00:00Z",
        }
    }

    kept, removed = garbage_collect_raw_cache(namespace_dir=namespace_dir, detection_cache=detection_cache, targets={})

    assert removed == ["deadbeef"]
    assert kept == {}
    assert outside.is_file()  # never touched: it resolves outside the cache root


def test_gc_ignores_discarded_variants_audit_records(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    namespace_dir = tmp_path / "ns"
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")

    outcome = reconcile_variants(
        target="guitar", requests=[detail], transcriber=_CountingFake(),
        source=source, input_hash=_input_hash(source), namespace_dir=namespace_dir,
    )
    dh = next(iter(outcome.detection_cache))

    # A discarded-variant audit record (no live `variants` entry) must not
    # keep the raw cache alive -- discard semantics never retain artifacts.
    targets = {
        "guitar": {
            "variants": {},
            "discarded_variants": [{"detection_hash": dh, "label": "detail"}],
        }
    }
    kept, removed = garbage_collect_raw_cache(namespace_dir=namespace_dir, detection_cache=outcome.detection_cache, targets=targets)

    assert removed == [dh]
    assert kept == {}


def test_detection_hash_and_cleanup_hash_are_deterministic_and_order_independent(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    detail = _variant("v-detail", "detail", "guitar-acoustic-detail")
    input_hash = _input_hash(source)

    first = detection_hash("guitar", input_hash, detail.spec)
    second = detection_hash("guitar", input_hash, detail.spec)
    assert first == second

    ch_first = cleanup_hash("raw-hash", input_hash, detail.spec)
    ch_second = cleanup_hash("raw-hash", input_hash, detail.spec)
    assert ch_first == ch_second
    assert cleanup_hash("other-raw-hash", input_hash, detail.spec) != ch_first


# --- artifact layout reconciliation (#223) ----------------------------------


def _legacy_target_record(target: str, variant_id: str, *, notes: bool = True, events: bool = False) -> dict[str, Any]:
    """A target record in the shape `sidecar.migrate_transcription_target`
    produces for a pre-v13 sidecar: a `default` variant carrying the flat
    artifact paths verbatim, with the record's own flat compatibility fields
    still naming the very same files."""
    variant: dict[str, Any] = {
        "label": "default",
        "status": "transcribed",
        "input_hash": "input-hash",
        "settings_hash": "settings-hash",
        "detection_hash": "detection-hash",
        "cleanup_hash": "cleanup-hash",
        "midi_file": f"transcription/{target}.mid",
        "notes_file": f"transcription/{target}.csv" if notes else None,
        "events_file": f"transcription/{target}.json" if events else None,
    }
    return {
        "status": "transcribed",
        "midi_file": variant["midi_file"],
        "notes_file": variant["notes_file"],
        "events_file": variant["events_file"],
        "variants": {variant_id: variant},
        "variant_order": [variant_id],
        "discarded_variants": [],
    }


def _write_artifact(namespace_dir: Path, name: str, content: bytes = b"artifact") -> Path:
    path = namespace_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_layout_relocates_a_migrated_flat_variant_and_rewrites_its_record(tmp_path: Path) -> None:
    """The case every pre-v13 project is in: `upgrade()` derived a `default`
    variant but deliberately left the files at their flat paths."""
    namespace_dir = tmp_path / "vgt" / "abc123"
    _write_artifact(namespace_dir, "transcription/guitar.mid", b"midi")
    _write_artifact(namespace_dir, "transcription/guitar.csv", b"notes")
    targets = {"guitar": _legacy_target_record("guitar", "ca81e27f")}

    outcome = reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets)

    variant = targets["guitar"]["variants"]["ca81e27f"]
    assert variant["midi_file"] == "transcription/guitar/ca81e27f.mid"
    assert variant["notes_file"] == "transcription/guitar/ca81e27f.csv"
    assert (namespace_dir / "transcription/guitar/ca81e27f.mid").read_bytes() == b"midi"
    assert (namespace_dir / "transcription/guitar/ca81e27f.csv").read_bytes() == b"notes"
    assert not (namespace_dir / "transcription/guitar.mid").exists()
    assert not (namespace_dir / "transcription/guitar.csv").exists()
    # The record's own compatibility view names the same artifacts and must
    # not be left pointing at a path that no longer exists.
    assert targets["guitar"]["midi_file"] == "transcription/guitar/ca81e27f.mid"
    assert outcome.records_changed
    # Relocating a file changes no identity, so nothing here may invalidate the
    # variant's cache and force a needless re-transcription.
    assert (variant["input_hash"], variant["settings_hash"], variant["detection_hash"], variant["cleanup_hash"]) == (
        "input-hash", "settings-hash", "detection-hash", "cleanup-hash",
    )
    assert variant["status"] == "transcribed"


def test_layout_sweeps_a_flat_orphan_no_live_variant_references(tmp_path: Path) -> None:
    """What a variant's first re-reconcile leaves behind: the record already
    points into the target directory, and the flat file is stranded."""
    namespace_dir = tmp_path / "vgt" / "abc123"
    _write_artifact(namespace_dir, "transcription/drums.mid", b"stale-midi")
    _write_artifact(namespace_dir, "transcription/drums.json", b"stale-events")
    _write_artifact(namespace_dir, "transcription/drums/791c654b.mid", b"current-midi")
    _write_artifact(namespace_dir, "transcription/drums/791c654b.json", b"current-events")
    targets = {
        "drums": {
            "variants": {
                "791c654b": {
                    "label": "default",
                    "status": "transcribed",
                    "midi_file": "transcription/drums/791c654b.mid",
                    "events_file": "transcription/drums/791c654b.json",
                }
            },
            "variant_order": ["791c654b"],
            "discarded_variants": [],
        }
    }

    outcome = reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets)

    assert not (namespace_dir / "transcription/drums.mid").exists()
    assert not (namespace_dir / "transcription/drums.json").exists()
    assert (namespace_dir / "transcription/drums/791c654b.mid").read_bytes() == b"current-midi"
    assert sorted(outcome.removed) == ["transcription/drums.json", "transcription/drums.mid"]
    assert not outcome.records_changed  # nothing recorded pointed at the orphans


def test_layout_never_sweeps_a_flat_artifact_a_live_variant_still_references(tmp_path: Path) -> None:
    """A record whose id the ReaScript would refuse to turn into a path is
    left strictly alone -- including its file, which stays referenced."""
    namespace_dir = tmp_path / "vgt" / "abc123"
    _write_artifact(namespace_dir, "transcription/bass.mid", b"midi")
    targets = {
        "bass": {
            "variants": {"../escape": {"label": "default", "status": "transcribed", "midi_file": "transcription/bass.mid"}},
            "variant_order": ["../escape"],
        }
    }

    outcome = reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets)

    assert (namespace_dir / "transcription/bass.mid").is_file()
    assert not list(namespace_dir.glob("transcription/bass/*"))
    assert outcome.removed == []


def test_layout_records_a_missing_flat_artifact_at_its_new_name_without_crashing(tmp_path: Path) -> None:
    namespace_dir = tmp_path / "vgt" / "abc123"
    (namespace_dir / "transcription").mkdir(parents=True)
    targets = {"guitar": _legacy_target_record("guitar", "ca81e27f")}

    messages: list[str] = []
    outcome = reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets, emit=messages.append)

    assert targets["guitar"]["variants"]["ca81e27f"]["midi_file"] == "transcription/guitar/ca81e27f.mid"
    assert outcome.missing == ["transcription/guitar/ca81e27f.mid", "transcription/guitar/ca81e27f.csv"]
    # Never silently: a dangling record is reported, not swallowed.
    assert any("is missing" in message for message in messages)


def test_layout_removes_only_empty_work_directories(tmp_path: Path) -> None:
    namespace_dir = tmp_path / "vgt" / "abc123"
    (namespace_dir / "transcription" / "_work-detection" / "deadbeef").mkdir(parents=True)
    (namespace_dir / "transcription" / "_work-guitar-abc").mkdir(parents=True)
    _write_artifact(namespace_dir, "transcription/_work-drums-xyz/in-flight.mid", b"scratch")

    outcome = reconcile_artifact_layout(namespace_dir=namespace_dir, targets={})

    assert not (namespace_dir / "transcription" / "_work-detection").exists()
    assert not (namespace_dir / "transcription" / "_work-guitar-abc").exists()
    assert (namespace_dir / "transcription" / "_work-drums-xyz" / "in-flight.mid").is_file()
    assert sorted(outcome.removed) == ["transcription/_work-detection/", "transcription/_work-guitar-abc/"]


def test_layout_reconciliation_is_idempotent(tmp_path: Path) -> None:
    namespace_dir = tmp_path / "vgt" / "abc123"
    _write_artifact(namespace_dir, "transcription/guitar.mid", b"midi")
    _write_artifact(namespace_dir, "transcription/guitar.csv", b"notes")
    _write_artifact(namespace_dir, "transcription/drums.mid", b"orphan")
    targets = {"guitar": _legacy_target_record("guitar", "ca81e27f")}

    reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets)
    before = sorted(str(path.relative_to(namespace_dir)) for path in namespace_dir.rglob("*") if path.is_file())

    second = reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets)

    after = sorted(str(path.relative_to(namespace_dir)) for path in namespace_dir.rglob("*") if path.is_file())
    assert before == after == ["transcription/guitar/ca81e27f.csv", "transcription/guitar/ca81e27f.mid"]
    assert not second.changed


def test_layout_catches_up_a_stale_record_whose_file_another_pass_already_moved(tmp_path: Path) -> None:
    """`analysis.apply_artifact_layout` runs this twice -- once on its
    in-memory analysis, once on the authoritative on-disk copy inside the
    atomic sidecar update -- so a record still naming the flat path after the
    file has moved must resolve to the new name, not be reported missing."""
    namespace_dir = tmp_path / "vgt" / "abc123"
    _write_artifact(namespace_dir, "transcription/guitar/ca81e27f.mid", b"midi")
    _write_artifact(namespace_dir, "transcription/guitar/ca81e27f.csv", b"notes")
    targets = {"guitar": _legacy_target_record("guitar", "ca81e27f")}

    outcome = reconcile_artifact_layout(namespace_dir=namespace_dir, targets=targets)

    assert targets["guitar"]["variants"]["ca81e27f"]["midi_file"] == "transcription/guitar/ca81e27f.mid"
    assert outcome.missing == []
    assert outcome.records_changed
