"""On-demand single-track MT3 job runner (docs/on-demand-track-transcription-plan.md).

This module *is* the backgrounded job: a REAPER trigger script renders one
arbitrary selected track, writes the job's initial ``status.json`` (job id,
source track name/GUID, item bounds, requested program), and spawns
``vgt transcription track run`` detached. There is no separate daemon --
this process's own lifetime is the job's lifetime, and because it is
detached, ``status.json`` is the *only* channel the user will ever see: the
whole body below is wrapped so that even an unexpected exception lands there
as ``"error"`` rather than vanishing silently with the process (see
``run_track_job``).

``status.json`` is a single-writer file by construction (see the plan's
"Exactly one writer per piece of state"): this module is its only writer
while a job is running, so it needs no lock, no merge protocol, and no
`generation` check -- only a temp-file + atomic ``os.replace`` so a reader
never observes a half-written file (mirrors ``sidecar.write_sidecar``). This
is *not* the project's ``.vgt`` sidecar; this module never reads it for
anything but the already-persisted tempo/tempo-map (read-only, once, up
front) and never calls ``analysis.analyze()``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
import json
import os
import platform
import shutil
import subprocess
import tempfile

from .mt3_normalize import (
    MT3_NOTE_NORMALIZATION_VERSION,
    MT3_TRACK_SELECTION_VERSION,
    merge_all_musical_tracks,
    write_normalized_mt3_artifacts,
)
from .sidecar import SidecarError, read_sidecar
from .transcribe import (
    MT3_TIMEOUT_SECONDS,
    BasicPitchTranscriber,
    EssentiaTranscriber,
    Mt3Spec,
    TranscriptionError,
    _bar_duration_seconds,
    _stderr_tail,
    _without_temporary_path,
    basic_pitch_spec_from_profile,
    build_mt3_argv,
    essentia_spec_from_profile,
    instrument_profile,
    tempo_map_reference,
)

NOTIFICATION_TITLE = "vgt"

# The on-demand track job's non-MT3 alternatives (see the plan's "profile
# picker" step, added on top of the original MT3-only design): each maps to
# a builtin registry profile, resolved through the exact same
# `*_spec_from_profile` helpers `vgt analyze` uses for its own targets, so a
# profile picked here and one picked through `--mode` never diverge. `mt3`
# is not in this map -- it stays on its own dedicated path in
# `run_track_job` below, since only MT3 has a multi-instrument decode to
# pin with `force_program`.
TRACK_JOB_BACKENDS: tuple[str, ...] = ("mt3", "guitar-klapuri", "guitar-melodia", "basic-pitch")
_PROFILE_NAME_FOR_BACKEND: dict[str, str] = {
    "guitar-klapuri": "guitar-klapuri",
    "guitar-melodia": "guitar-melodia",
    "basic-pitch": "default",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(job_dir: Path, **fields: Any) -> dict[str, Any]:
    """Merge `fields` onto whatever is currently in `job_dir/status.json` and
    replace it atomically. The job process is this file's only writer (see
    the module docstring) -- no lock, no `generation` check, no merge
    protocol beyond "read what's there, then win"."""
    path = job_dir / "status.json"
    current = _read_status(path)
    current.update(fields)
    fd, tmp_path = tempfile.mkstemp(dir=str(job_dir), prefix=".status.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(current, indent=2) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return current


def _notify(message: str, *, title: str = NOTIFICATION_TITLE) -> None:
    """Best-effort OS notification (macOS only, matching this project's
    stated macOS-only environment assumption). Never raises: a missing
    notification permission silently suppresses `display notification` with
    no error, so this is never the only channel a caller relies on -- the
    durable outcome is always `status.json` (see the module docstring)."""
    if platform.system() != "Darwin":
        return
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001 -- best-effort by design, see docstring
        pass


def _build_spec(*, midi_tempo: float | None, tempo_map: Any, force_program: int, checkpoint_fingerprint: str | None) -> Mt3Spec:
    from .mt3_provision import (
        MT3_INPUT_LENGTH_FRAMES,
        MT3_LOCK_SHA256,
        MT3_LOOKAHEAD_FRAMES,
        MT3_MODEL_ID,
        MT3_PINNED_COMMIT,
        MT3_PINNED_TAG,
        MT3_REPO_URL,
        MT3_RUNTIME_VERSION,
    )
    return Mt3Spec(
        backend="mt3", repository=MT3_REPO_URL, tag=MT3_PINNED_TAG, commit=MT3_PINNED_COMMIT,
        runtime_version=MT3_RUNTIME_VERSION, lock_sha256=MT3_LOCK_SHA256, model_id=MT3_MODEL_ID,
        input_length_frames=MT3_INPUT_LENGTH_FRAMES, lookahead_frames=MT3_LOOKAHEAD_FRAMES,
        checkpoint_fingerprint=checkpoint_fingerprint,
        track_selection_version=MT3_TRACK_SELECTION_VERSION, note_normalization_version=MT3_NOTE_NORMALIZATION_VERSION,
        target=None, midi_tempo=midi_tempo, tempo_map=tempo_map, force_program=force_program,
    )


def _run_mt3(source: Path, spec: Mt3Spec, *, work_dir: Path) -> Path:
    """Invoke the pinned `mt3-transcribe` CLI and return its raw multi-track
    output path. Mirrors `Mt3Transcriber.detect_raw`'s inner subprocess
    invocation exactly, without that method's target-specific
    `select_dominant_musical_track` step -- this job has no target (see
    `Mt3Spec.target`'s docstring)."""
    from .mt3_provision import default_cache_dir

    cache_dir = default_cache_dir()
    repo_dir = cache_dir / "repo"
    checkpoint_dir = cache_dir / "models" / "checkpoint_0"
    if not checkpoint_dir.is_dir():
        raise TranscriptionError("mt3 checkpoint directory is missing; run `vgt transcription backend provision mt3` again")

    raw_output = work_dir / "mt3_raw.mid"
    argv = build_mt3_argv(
        source, raw_output, checkpoint_dir, repo_dir,
        input_length_frames=spec.input_length_frames, lookahead_frames=spec.lookahead_frames,
        force_program=spec.force_program,
    )
    try:
        completed = subprocess.run(
            argv, cwd=work_dir, capture_output=True, text=True, timeout=MT3_TIMEOUT_SECONDS, errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError(f"mt3-transcribe timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise TranscriptionError(f"failed to run mt3-transcribe: {exc}") from exc
    if completed.returncode != 0:
        context = _stderr_tail(completed.stderr) or _stderr_tail(completed.stdout)
        raise TranscriptionError(
            _without_temporary_path(f"mt3-transcribe exited with status {completed.returncode}: {context}", work_dir)
        )
    if not raw_output.is_file():
        raise TranscriptionError("mt3-transcribe reported success but wrote no output file")
    return raw_output


def _resolve_tempo(project: str | Path) -> tuple[float | None, Any, str | None]:
    """The project's already-analyzed tempo/tempo-map/time-signature, read-only.

    Never calls `analysis.analyze()`: a track job runs entirely off what
    `vgt analyze` already persisted (see the plan's "Independence from
    `vgt apply`"). Fails clearly, rather than falling back to a bare 120 BPM
    guess, when no analyzed tempo is on record yet. The time signature is
    used only by the non-MT3 backends' bar-based cleanup (see
    `_run_profile_backend`); MT3 has no such cleanup and ignores it.
    """
    try:
        sidecar = read_sidecar(project)
    except SidecarError as exc:
        raise TranscriptionError(str(exc)) from exc
    tempo_value = sidecar["analysis"]["tempo"].get("value")
    if not isinstance(tempo_value, dict) or tempo_value.get("bpm") is None:
        raise TranscriptionError(
            "no analyzed tempo is on record for this project; run `vgt analyze` at least once before "
            "transcribing an arbitrary track"
        )
    return tempo_value.get("bpm"), tempo_map_reference(tempo_value), tempo_value.get("time_signature")


def _run_profile_backend(
    source: Path,
    backend: str,
    *,
    midi_tempo: float | None,
    tempo_map: Any,
    time_signature: str | None,
    work_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, Path, int]:
    """Run one of the non-MT3 on-demand backends and return its raw MIDI/CSV
    paths and note count.

    Unlike MT3's `force_program` (see `_build_spec`), Basic Pitch and
    Essentia have no multi-instrument classification to pin: neither backend
    is even aware of the user's requested GM program. That choice is
    recorded in `status.json` for labelling only (see `run_track_job`) and
    never reaches these specs.
    """
    profile = instrument_profile(_PROFILE_NAME_FOR_BACKEND[backend])
    bar_seconds = _bar_duration_seconds(midi_tempo, time_signature)
    sustain_clamp_s = bar_seconds * profile.sustain_clamp_bars if bar_seconds else None
    if profile.backend == "essentia":
        spec = essentia_spec_from_profile(
            profile, midi_tempo=midi_tempo, sustain_clamp_s=sustain_clamp_s, tempo_map=tempo_map,
        )
        result = EssentiaTranscriber().transcribe(source, work_dir, spec, progress)
    else:
        spec = basic_pitch_spec_from_profile(
            profile, midi_tempo=midi_tempo, sustain_clamp_s=sustain_clamp_s, tempo_map=tempo_map,
        )
        result = BasicPitchTranscriber().transcribe(source, work_dir, spec, progress)
    return result.midi_path, result.notes_path, result.note_count


def run_track_job(
    project: str | Path,
    job_id: str,
    *,
    source: Path,
    force_program: int,
    backend: str = "mt3",
    label: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one on-demand single-track transcription job to completion and
    return its final `status.json` contents.

    `backend` selects between the original MT3 path (default, unchanged) and
    the non-MT3 alternatives in `TRACK_JOB_BACKENDS`/`_PROFILE_NAME_FOR_BACKEND`
    -- Essentia's `guitar-klapuri`/`guitar-melodia` multi-pitch estimators, or
    plain Basic Pitch -- run through `_run_profile_backend`. Only MT3 ever
    reads `force_program`: the other backends have no multi-instrument
    decode to pin (see `_run_profile_backend`'s docstring); `force_program`
    is still recorded in `status.json` for every backend, for track-label
    purposes only.

    Never raises: every failure mode (missing analyzed tempo, missing
    provisioning, a bad subprocess exit, a malformed output file, or any
    other unexpected exception) is caught and recorded as `status: "error"`
    in `status.json`, because this process is detached and nothing else
    will ever see it fail (see the module docstring). The OS notification
    fires last, success or failure, and is itself never allowed to raise.
    """
    emit = progress or (lambda _message: None)
    job_dir = source.parent
    write_status(job_dir, status="running", job_id=job_id, label=label, backend=backend, started_at=_now_iso())

    try:
        midi_tempo, tempo_map, time_signature = _resolve_tempo(project)
        if backend == "mt3":
            emit(f"transcribing (on-demand mt3): {source.name}")
            from .mt3_provision import Mt3ProvisionError, require_mt3_provisioned

            try:
                checkpoint_fingerprint = require_mt3_provisioned().fingerprint
            except Mt3ProvisionError as exc:
                raise TranscriptionError(str(exc)) from exc
            spec = _build_spec(
                midi_tempo=midi_tempo, tempo_map=tempo_map, force_program=force_program,
                checkpoint_fingerprint=checkpoint_fingerprint,
            )
            with tempfile.TemporaryDirectory(prefix="vgt-track-job-") as temporary:
                work_dir = Path(temporary)
                raw_output = _run_mt3(source, spec, work_dir=work_dir)
                try:
                    selected = merge_all_musical_tracks(raw_output)
                except TranscriptionError as exc:
                    raise TranscriptionError(_without_temporary_path(str(exc), work_dir)) from exc
                midi_path = job_dir / "result.mid"
                notes_path = job_dir / "result.csv"
                write_normalized_mt3_artifacts(
                    selected, csv_path=notes_path, midi_path=midi_path, tempo_bpm=midi_tempo or 120.0, tempo_map=tempo_map,
                )
            note_count = len(selected.notes)
        elif backend in _PROFILE_NAME_FOR_BACKEND:
            emit(f"transcribing (on-demand {backend}): {source.name}")
            with tempfile.TemporaryDirectory(prefix="vgt-track-job-") as temporary:
                work_dir = Path(temporary)
                try:
                    raw_midi_path, raw_notes_path, note_count = _run_profile_backend(
                        source, backend, midi_tempo=midi_tempo, tempo_map=tempo_map, time_signature=time_signature,
                        work_dir=work_dir, progress=progress,
                    )
                except TranscriptionError as exc:
                    raise TranscriptionError(_without_temporary_path(str(exc), work_dir)) from exc
                midi_path = job_dir / "result.mid"
                notes_path = job_dir / "result.csv"
                shutil.copyfile(raw_midi_path, midi_path)
                shutil.copyfile(raw_notes_path, notes_path)
        else:
            raise TranscriptionError(f"unknown track job backend {backend!r}; must be one of {TRACK_JOB_BACKENDS}")

        emit(f"done (on-demand {backend}): {note_count} notes")
        status = write_status(
            job_dir, status="done", finished_at=_now_iso(), note_count=note_count, error=None,
        )
        _notify(f"Transcription ready: {label or source.stem} ({note_count} notes)")
        return status
    except TranscriptionError as exc:
        status = write_status(job_dir, status="error", finished_at=_now_iso(), error=str(exc))
        _notify(f"Transcription failed: {label or source.stem}")
        return status
    except Exception as exc:  # noqa: BLE001 -- last-resort capture, see docstring
        status = write_status(job_dir, status="error", finished_at=_now_iso(), error=f"unexpected error: {exc}")
        _notify(f"Transcription failed: {label or source.stem}")
        return status
