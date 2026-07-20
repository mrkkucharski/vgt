from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json
import wave

import httpx
import pytest

from vgt.lalal import AmbiguousSubmissionError, InsufficientMinutesError, LalalSeparator
from vgt.separation import build_recipe


LICENSE_KEY = "test-license-key-must-never-leak"


def _wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://www.lalal.ai")


def test_v1_split_uploads_submits_polls_and_streams_wavs(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    wav = _wav_bytes()

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
        assert request.url.path == "/api/v1/limits/minutes_left/"
        return httpx.Response(200, json={"minutes_left": 0})

    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    with pytest.raises(InsufficientMinutesError):
        separator.preflight(source=source, outstanding_operations=5)


def test_cancel_and_delete_use_v1_endpoints_only() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"success": True})

    separator = LalalSeparator(license_key=LICENSE_KEY, client=_client(handler))
    separator.cancel(["task-1"])
    separator.delete("source-1")
    assert paths == ["/api/v1/cancel/", "/api/v1/delete/"]
