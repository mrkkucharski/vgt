from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json
import wave

import httpx
import pytest

from vgt.lalal import AmbiguousSubmissionError, InsufficientMinutesError, LalalError, LalalSeparator
from vgt.separation import build_recipe


LICENSE_KEY = "test-license-key-must-never-leak"


def _wav_bytes(seconds: float = 0.1) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return output.getvalue()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://www.lalal.ai")


def test_v1_split_uploads_submits_polls_and_streams_wavs(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    wav = _wav_bytes(6)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.lalal.ai":
            assert request.headers["X-License-Key"] == LICENSE_KEY
            assert "Authorization" not in request.headers
        if request.url.path == "/api/v1/upload/":
            assert request.headers["Content-Disposition"] == 'attachment; filename="source.wav"'
            return httpx.Response(200, json={"id": "source-1", "duration": 6, "expires": 4102444800, "name": "x", "size": 10})
        if request.url.path == "/api/v1/split/stem_separator/":
            payload = json.loads(request.content)
            assert payload["idempotency_key"]
            assert payload["presets"] == {
                "stem": "vocals", "splitter": "auto", "dereverb_enabled": False,
                "extraction_level": "deep_extraction", "encoder_format": "wav",
            }
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/api/v1/check/":
            return httpx.Response(200, json={"result": {"task-1": {
                "status": "success", "presets": {"splitter": "Perseus", "encoder_format": "wav"},
                "result": {"duration": 6, "tracks": [
                    {"type": "stem", "url": "https://download.test/stem"},
                    {"type": "back", "url": "https://download.test/back"},
                ]},
            }}})
        assert request.url.host == "download.test"
        return httpx.Response(200, content=wav, headers={"Content-Length": str(len(wav))})

    source = tmp_path / "source.wav"
    source.write_bytes(wav)
    checkpoints: list[dict] = []
    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler), sleep=lambda _: None)
    result = separator.split(source, tmp_path / "out", build_recipe("electric")["vocals-original"].spec,
                             resume_state=None, checkpoint=lambda state: checkpoints.append(dict(state)))

    assert set(result.outputs) == {"stem", "back"}
    assert result.effective_presets["splitter"] == "Perseus"
    assert checkpoints[0]["source_id"] == "source-1"
    assert checkpoints[1]["idempotency_key"] and "task_id" not in checkpoints[1]
    assert checkpoints[2]["task_id"] == "task-1"
    serialized = json.dumps(checkpoints)
    assert LICENSE_KEY not in serialized
    assert all("/api/v1/split/multistem/" != str(request.url.path) for request in seen)


def test_ambiguous_submission_fails_closed_without_a_second_request(tmp_path: Path) -> None:
    wav = _wav_bytes()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/api/v1/upload/":
            return httpx.Response(200, json={"id": "source-1", "duration": 6, "expires": 4102444800, "name": "x", "size": 10})
        return httpx.Response(503)

    source = tmp_path / "source.wav"
    source.write_bytes(wav)
    state: dict = {}
    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    with pytest.raises(AmbiguousSubmissionError) as first:
        separator.split(source, tmp_path / "out", build_recipe("electric")["bass-original"].spec,
                        resume_state=None, checkpoint=lambda update: state.update(update))
    assert LICENSE_KEY not in str(first.value)
    calls_after_first = calls
    with pytest.raises(AmbiguousSubmissionError):
        separator.split(source, tmp_path / "out", build_recipe("electric")["bass-original"].spec,
                        resume_state=state, checkpoint=lambda update: state.update(update))
    assert calls == calls_after_first


def test_preflight_refuses_insufficient_minutes_before_paid_work(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(_wav_bytes())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/upload/":
            return httpx.Response(200, json={"id": "source-1", "duration": 6, "expires": 4102444800})
        assert request.url.path == "/api/v1/limits/minutes_left/"
        return httpx.Response(200, json={"minutes_left": 0})

    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    with pytest.raises(InsufficientMinutesError):
        separator.preflight(source=source, outstanding_operations=5)


def test_preflight_uses_uploaded_duration_when_local_decoder_cannot_read_source(tmp_path: Path) -> None:
    source = tmp_path / "source.unknown"
    source.write_bytes(b"not locally decodable")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/upload/":
            return httpx.Response(200, json={"id": "source-1", "duration": 120, "expires": 4102444800})
        return httpx.Response(200, json={"minutes_left": 9.9})

    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    with pytest.raises(InsufficientMinutesError):
        separator.preflight(source=source, outstanding_operations=5)
    assert paths == ["/api/v1/upload/", "/api/v1/limits/minutes_left/"]


def test_preflight_reuses_an_unexpired_checkpointed_upload(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(_wav_bytes())
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/api/v1/limits/minutes_left/"
        return httpx.Response(200, json={"minutes_left": 1})

    state = {"source_id": "source-1", "source_expires": 4102444800, "source_duration_seconds": 6}
    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    result = separator.preflight(sources=[(source, 5)], source_states=[state])

    assert result == [state]
    assert paths == ["/api/v1/limits/minutes_left/"]


def test_expired_task_reconstructs_with_a_new_durable_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(_wav_bytes())
    wav = _wav_bytes()
    submitted: list[dict] = []
    checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal checks
        if request.url.path == "/api/v1/check/":
            checks += 1
            if checks == 1:
                return httpx.Response(404)
            return httpx.Response(200, json={"result": {"replacement": {
                "status": "success", "presets": {"splitter": "Perseus"},
                "result": {"duration": 0.1, "tracks": [{"type": "stem", "url": "https://download.test/stem"}]},
            }}})
        if request.url.path == "/api/v1/split/stem_separator/":
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"task_id": "replacement"})
        assert request.url.host == "download.test"
        return httpx.Response(200, content=wav, headers={"Content-Length": str(len(wav))})

    initial = {"source_id": "source-1", "source_expires": 4102444800, "task_id": "expired", "idempotency_key": "old"}
    checkpoints: list[dict] = []
    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler), sleep=lambda _: None)
    result = separator.split(source, tmp_path / "out", build_recipe("electric")["bass-original"].spec,
                             resume_state=initial, checkpoint=lambda state: checkpoints.append(dict(state)))

    assert result.backend_state["expired_task_id"] == "expired"
    assert result.backend_state["task_id"] == "replacement"
    assert submitted[0]["idempotency_key"] != "old"
    assert any(state.get("status") == "reconstructing" and state.get("idempotency_key") != "old" for state in checkpoints)


def test_download_rejects_wav_with_implausible_task_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(_wav_bytes())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/upload/":
            return httpx.Response(200, json={"id": "source-1", "duration": 0.1, "expires": 4102444800})
        if request.url.path == "/api/v1/split/stem_separator/":
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/api/v1/check/":
            return httpx.Response(200, json={"result": {"task-1": {
                "status": "success", "result": {"duration": 60, "tracks": [{"type": "stem", "url": "https://download.test/stem"}]},
            }}})
        return httpx.Response(200, content=_wav_bytes(), headers={"Content-Length": str(len(_wav_bytes()))})

    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    with pytest.raises(LalalError, match="duration is implausible"):
        separator.split(source, tmp_path / "out", build_recipe("electric")["bass-original"].spec,
                        resume_state=None, checkpoint=lambda _: None)
    assert not (tmp_path / "out" / "stem.wav").exists()


def test_cancel_and_delete_use_v1_endpoints_only() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"success": True})

    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    separator.cancel(["task-1"])
    separator.delete("source-1")
    assert paths == ["/api/v1/cancel/", "/api/v1/delete/"]


def test_reconstruction_pins_the_effective_auto_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["presets"]["splitter"] == "Perseus"
        return httpx.Response(200, json={"task_id": "replacement-task"})

    state = {
        "source_id": "source-1", "source_expires": 4102444800,
        "effective_presets": {"splitter": "Perseus"},
    }
    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    separator._submit(state, build_recipe("electric")["bass-original"].spec, lambda _: None)
    assert state["task_id"] == "replacement-task"
