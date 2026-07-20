"""LALAL.AI API v1 implementation of the :class:`vgt.separation.Separator` seam.

This module deliberately contains no recipe policy.  It executes exactly one
already-selected split, and publishes every remote identifier as soon as it is
known so the orchestrator can resume it without another paid submission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
import os
import time
import uuid
import wave

import httpx
import soundfile as sf

from .separation import SplitResult, SplitSpec


class LalalError(RuntimeError):
    """A safe-to-display LALAL API failure (never includes credentials)."""


class AmbiguousSubmissionError(LalalError):
    """A split request may have been charged but did not yield a task id.

    Retrying this automatically could make a second paid task, so callers must
    stop and have an account owner recover the task identity.
    """


class InsufficientMinutesError(LalalError):
    """The account cannot cover all outstanding paid operations."""


class LalalSeparator:
    """Network-only LALAL API v1 separator.

    ``client`` and ``sleep`` are injectable solely to make CI fixture based;
    production callers use the normal HTTP client and bounded polling sleep.
    The license key is read only from ``LALAL_LICENSE_KEY`` and is retained in
    this object's request headers, never in returned state or exceptions.
    """

    name = "lalal"
    api_version = "v1"
    base_url = "https://www.lalal.ai"
    poll_initial_seconds = 3.0
    poll_max_seconds = 15.0  # <= 4 checks/min, comfortably below 30/min

    def __init__(
        self,
        *,
        license_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = license_key if license_key is not None else os.environ.get("LALAL_LICENSE_KEY")
        if not key:
            raise LalalError("LALAL_LICENSE_KEY is required for LALAL API v1 separation")
        self._license_key = key
        self._client = client or httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(60.0))
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LalalSeparator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _redact(self, value: object) -> str:
        return str(value).replace(self._license_key, "[REDACTED]")

    def _headers(self) -> dict[str, str]:
        return {"X-License-Key": self._license_key}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise LalalError(f"LALAL API request failed: {self._redact(exc)}") from exc
        if response.is_error:
            # Do not include response/request dumps: they can echo request headers.
            raise LalalError(f"LALAL API {method} {path} returned HTTP {response.status_code}")
        return response

    @staticmethod
    def _expired(state: dict[str, Any]) -> bool:
        try:
            return int(state["source_expires"]) <= int(time.time())
        except (KeyError, TypeError, ValueError):
            return True

    @staticmethod
    def _local_duration_seconds(source: Path) -> float:
        try:
            info = sf.info(str(source))
            return info.frames / info.samplerate
        except (RuntimeError, OSError, ZeroDivisionError):
            # This is only an early safety estimate.  LALAL's upload duration
            # becomes authoritative before the operation is submitted.
            return 0.0

    def preflight(self, *, source: Path, outstanding_operations: int) -> None:
        """Refuse before any paid task when credits cannot cover this run.

        WAV sources provide an exact local estimate. Other formats are checked
        again against LALAL's authoritative upload duration before submission.
        """
        minutes_left = float(self._request("POST", "/api/v1/limits/minutes_left/").json()["minutes_left"])
        needed = self._local_duration_seconds(source) * outstanding_operations / 60.0
        if needed and minutes_left < needed:
            raise InsufficientMinutesError(
                f"LALAL has {minutes_left:.2f} minutes left but this run needs {needed:.2f}; no tasks were submitted"
            )

    def _upload(self, source: Path, state: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        if state.get("source_id") and not self._expired(state):
            return state
        headers = {"Content-Disposition": f'attachment; filename="{source.name}"'}
        try:
            with source.open("rb") as stream:
                response = self._client.post(
                    "/api/v1/upload/", headers={**self._headers(), **headers}, content=stream
                )
        except (OSError, httpx.HTTPError) as exc:
            raise LalalError(f"LALAL upload failed: {self._redact(exc)}") from exc
        if response.is_error:
            raise LalalError(f"LALAL upload returned HTTP {response.status_code}")
        try:
            body = response.json()
            uploaded = {
                "source_id": str(body["id"]),
                "source_expires": int(body["expires"]),
                "source_duration_seconds": float(body["duration"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise LalalError("LALAL upload response was missing source identity") from exc
        state.update(uploaded)
        checkpoint(state)
        return state

    def _submit(self, state: dict[str, Any], spec: SplitSpec, checkpoint: Callable[[dict[str, Any]], None]) -> None:
        if state.get("task_id"):
            return
        # This checkpoint is intentionally before the request.  If the process
        # dies afterwards, state says a request was intended but does not claim
        # a recoverable task id; the next run fails closed rather than retries.
        state["idempotency_key"] = state.get("idempotency_key") or str(uuid.uuid4())
        state["submission_started"] = True
        checkpoint(state)
        payload = {
            "source_id": state["source_id"],
            "idempotency_key": state["idempotency_key"],
            "presets": {
                "stem": spec.stem,
                "splitter": spec.splitter,
                "dereverb_enabled": spec.dereverb_enabled,
                "extraction_level": spec.extraction_level,
                "encoder_format": "wav",
            },
        }
        try:
            response = self._client.post("/api/v1/split/stem_separator/", headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise AmbiguousSubmissionError(
                "LALAL split submission outcome is unknown; task was not retried to avoid a second charge"
            ) from exc
        if response.is_error:
            raise AmbiguousSubmissionError(
                f"LALAL split submission returned HTTP {response.status_code}; task was not retried to avoid a second charge"
            )
        try:
            state["task_id"] = str(response.json()["task_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AmbiguousSubmissionError(
                "LALAL accepted split submission without a task id; task was not retried to avoid a second charge"
            ) from exc
        checkpoint(state)  # task identity is durable before any polling/download

    def _check(self, task_id: str) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/check/", json={"task_ids": [task_id]})
        try:
            return response.json()["result"][task_id]
        except (KeyError, TypeError) as exc:
            raise LalalError("LALAL check response did not contain the submitted task") from exc

    def _poll(self, task_id: str, checkpoint: Callable[[dict[str, Any]], None], state: dict[str, Any]) -> dict[str, Any]:
        delay = self.poll_initial_seconds
        while True:
            try:
                result = self._check(task_id)
            except LalalError as exc:
                # A rate limit is retryable. Other errors (including expired
                # task/source) remain explicit and never cause resubmission.
                if "HTTP 429" not in str(exc):
                    raise
                self._sleep(delay)
                delay = min(delay * 2, self.poll_max_seconds)
                continue
            status = result.get("status")
            state["remote_status"] = status
            state["effective_presets"] = result.get("presets") or state.get("effective_presets") or {}
            checkpoint(state)
            if status == "success":
                return result
            if status in {"error", "cancelled", "server_error"}:
                raise LalalError(f"LALAL task {status}: {self._redact(result.get('error', 'no details'))}")
            if status != "progress":
                raise LalalError(f"LALAL task returned unknown status {status!r}")
            self._sleep(delay)
            delay = min(delay * 2, self.poll_max_seconds)

    def _download(self, url: str, destination: Path) -> bool:
        part = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code in {403, 404}:
                    return False  # signed URL expired; caller refreshes via /check/
                if response.is_error:
                    raise LalalError(f"LALAL download returned HTTP {response.status_code}")
                expected = response.headers.get("Content-Length")
                written = 0
                with part.open("wb") as output:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        output.write(chunk)
            if expected is not None and written != int(expected):
                raise LalalError("LALAL download length did not match Content-Length")
            try:
                with wave.open(str(part), "rb") as handle:
                    if handle.getnframes() <= 0 or handle.getframerate() <= 0:
                        raise LalalError("LALAL download has no readable WAV audio")
            except (wave.Error, EOFError, OSError) as exc:
                raise LalalError("LALAL download is not a readable WAV file") from exc
            part.replace(destination)
            return True
        except httpx.HTTPError as exc:
            raise LalalError(f"LALAL download failed: {self._redact(exc)}") from exc
        finally:
            if part.exists():
                part.unlink(missing_ok=True)

    def cancel(self, task_ids: list[str]) -> None:
        self._request("POST", "/api/v1/cancel/", json={"task_ids": task_ids})

    def delete(self, source_id: str) -> None:
        self._request("POST", "/api/v1/delete/", json={"source_id": source_id})

    def split(self, source: Path, out_dir: Path, spec: SplitSpec, *, resume_state: dict[str, Any] | None,
              checkpoint: Callable[[dict[str, Any]], None]) -> SplitResult:
        state = dict(resume_state or {})
        # A prior process reached the dangerous window after submitting intent
        # but before a durable task id. No endpoint can safely identify it.
        if state.get("submission_started") and not state.get("task_id"):
            raise AmbiguousSubmissionError(
                "Previous LALAL submission has no recorded task id; refusing automatic retry to avoid a second charge"
            )
        self._upload(source, state, checkpoint)
        self._submit(state, spec, checkpoint)
        result = self._poll(state["task_id"], checkpoint, state)
        tracks = {track.get("type"): track.get("url") for track in result.get("result", {}).get("tracks", [])}
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}
        for side in spec.keep:
            url = tracks.get(side)
            if not isinstance(url, str):
                raise LalalError(f"LALAL completed task without required {side} track")
            target = out_dir / f"{side}.wav"
            if not self._download(url, target):
                result = self._poll(state["task_id"], checkpoint, state)
                tracks = {track.get("type"): track.get("url") for track in result.get("result", {}).get("tracks", [])}
                refreshed = tracks.get(side)
                if not isinstance(refreshed, str) or not self._download(refreshed, target):
                    raise LalalError("LALAL download URL expired and could not be refreshed")
            outputs[side] = target
        state["status"] = "completed"
        checkpoint(state)
        return SplitResult(outputs=outputs, backend_state=state, effective_presets=dict(state.get("effective_presets") or {}))
