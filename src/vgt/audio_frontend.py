"""Deterministic, analysis-only audio frontends for transcription variants.

Raw LALAL stems are never modified.  A frontend writes a cacheable derivative
whose identity is the raw content hash plus this module's recipe/version.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json

import numpy as np
import soundfile as sf

ALGORITHM_VERSION = 1


class AudioFrontendError(ValueError):
    pass


def canonical_recipe(recipe: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the small, deliberately conservative frontend surface."""
    if recipe is None:
        return {"stages": []}
    if not isinstance(recipe, dict) or set(recipe) != {"stages"} or not isinstance(recipe["stages"], list):
        raise AudioFrontendError("audio_frontend must contain exactly a stages list")
    stages: list[dict[str, Any]] = []
    previous = -1
    order = {"bandpass": 0, "hpss_blend": 1, "soft_gate": 2}
    for item in recipe["stages"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise AudioFrontendError("each audio_frontend stage needs a type")
        kind = item["type"]
        if kind not in order or order[kind] <= previous:
            raise AudioFrontendError("audio_frontend stages must be bandpass, hpss_blend, soft_gate in that order")
        previous = order[kind]
        if kind == "bandpass":
            allowed = {"type", "low_hz", "high_hz", "order"}
            if set(item) - allowed or not isinstance(item.get("low_hz"), (int, float)) or not isinstance(item.get("high_hz"), (int, float)):
                raise AudioFrontendError("bandpass needs low_hz and high_hz")
            low, high, filt_order = float(item["low_hz"]), float(item["high_hz"]), int(item.get("order", 4))
            if low <= 0 or high <= low or filt_order < 1:
                raise AudioFrontendError("bandpass bounds/order are invalid")
            stages.append({"type": kind, "low_hz": low, "high_hz": high, "order": filt_order})
        elif kind == "hpss_blend":
            allowed = {"type", "component", "wet", "margin", "n_fft", "hop_length"}
            component = item.get("component")
            wet = item.get("wet")
            if set(item) - allowed or component not in {"harmonic", "percussive"} or not isinstance(wet, (int, float)):
                raise AudioFrontendError("hpss_blend needs component and wet")
            wet_value = float(wet)
            margin, n_fft, hop = float(item.get("margin", 1.0)), int(item.get("n_fft", 2048)), int(item.get("hop_length", 512))
            if not 0 <= wet_value <= 1 or margin < 1 or n_fft < 32 or hop < 1 or hop > n_fft:
                raise AudioFrontendError("hpss_blend parameters are invalid")
            stages.append({"type": kind, "component": component, "wet": wet_value, "margin": margin, "n_fft": n_fft, "hop_length": hop})
        else:
            allowed = {"type", "threshold_dbfs", "reduction_db", "frame_length", "hop_length", "attack_ms", "release_ms"}
            required = {"threshold_dbfs", "reduction_db", "attack_ms", "release_ms"}
            if set(item) - allowed or not required.issubset(item):
                raise AudioFrontendError("soft_gate needs threshold_dbfs, reduction_db, attack_ms and release_ms")
            threshold, reduction = float(item["threshold_dbfs"]), float(item["reduction_db"])
            frame, hop, attack, release = int(item.get("frame_length", 1024)), int(item.get("hop_length", 256)), float(item["attack_ms"]), float(item["release_ms"])
            if not -120 <= threshold <= 0 or reduction < 0 or frame < 32 or not 1 <= hop <= frame or attack <= 0 or release <= 0:
                raise AudioFrontendError("soft_gate parameters are invalid")
            stages.append({"type": kind, "threshold_dbfs": threshold, "reduction_db": reduction, "frame_length": frame, "hop_length": hop, "attack_ms": attack, "release_ms": release})
    return {"stages": stages}


def frontend_hash(raw_hash: str, recipe: dict[str, Any] | None) -> str:
    payload = {"algorithm_version": ALGORITHM_VERSION, "raw_input_hash": raw_hash, "recipe": canonical_recipe(recipe)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def frontend_relative_path(key: str) -> str:
    return f"transcription/cache/audio-frontends/{key}/analysis.wav"


def _bandpass(audio: np.ndarray, sr: int, stage: dict[str, Any]) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt
    if stage["high_hz"] >= sr / 2:
        raise AudioFrontendError("bandpass high_hz must be below the Nyquist frequency")
    sos = butter(stage["order"], [stage["low_hz"], stage["high_hz"]], btype="bandpass", fs=sr, output="sos")
    return sosfiltfilt(sos, audio, axis=0).astype(np.float32)


def _hpss(audio: np.ndarray, stage: dict[str, Any]) -> np.ndarray:
    import librosa
    out = np.empty_like(audio)
    for channel in range(audio.shape[1]):
        harmonic, percussive = librosa.effects.hpss(audio[:, channel], margin=stage["margin"], n_fft=stage["n_fft"], hop_length=stage["hop_length"])
        selected = harmonic if stage["component"] == "harmonic" else percussive
        out[:, channel] = (1.0 - stage["wet"]) * audio[:, channel] + stage["wet"] * selected
    return out


def _soft_gate(audio: np.ndarray, sr: int, stage: dict[str, Any]) -> np.ndarray:
    frame, hop = stage["frame_length"], stage["hop_length"]
    mono = audio.mean(axis=1)
    count = max(1, 1 + max(0, len(mono) - frame) // hop)
    rms = np.array([np.sqrt(np.mean(mono[i * hop:i * hop + frame] ** 2) + 1e-12) for i in range(count)])
    db = 20 * np.log10(rms + 1e-12)
    target = np.where(db < stage["threshold_dbfs"], 10 ** (-stage["reduction_db"] / 20), 1.0)
    attack = max(1, int(stage["attack_ms"] * sr / (1000 * hop)))
    release = max(1, int(stage["release_ms"] * sr / (1000 * hop)))
    smooth = np.empty_like(target)
    smooth[0] = target[0]
    for i in range(1, len(target)):
        alpha = 1 / (attack if target[i] < smooth[i - 1] else release)
        smooth[i] = smooth[i - 1] + alpha * (target[i] - smooth[i - 1])
    positions = np.minimum(np.arange(len(audio)) // hop, len(smooth) - 1)
    return audio * smooth[positions, None]


def render(source: Path, destination: Path, recipe: dict[str, Any] | None) -> dict[str, Any]:
    """Render an aligned PCM derivative atomically and return its metadata."""
    normalized = canonical_recipe(recipe)
    audio, sr = sf.read(str(source), always_2d=True, dtype="float32")
    original_shape = audio.shape
    for stage in normalized["stages"]:
        audio = _bandpass(audio, sr, stage) if stage["type"] == "bandpass" else _hpss(audio, stage) if stage["type"] == "hpss_blend" else _soft_gate(audio, sr, stage)
    if audio.shape != original_shape or not np.isfinite(audio).all():
        raise AudioFrontendError("frontend did not preserve finite aligned audio")
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.999:
        audio *= 0.999 / peak
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".wav.part")
    sf.write(str(partial), audio, sr, subtype="PCM_16", format="WAV")
    partial.replace(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {"file": frontend_relative_path(destination.parent.name), "sha256": digest, "sample_rate_hz": sr, "channels": audio.shape[1], "frame_count": audio.shape[0], "duration_seconds": audio.shape[0] / sr, "recipe": normalized}
