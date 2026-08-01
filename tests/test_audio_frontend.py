from pathlib import Path

import numpy as np
import soundfile as sf

from vgt.audio_frontend import AudioFrontendError, canonical_recipe, frontend_hash, render


def _source(tmp_path: Path) -> Path:
    sr = 22050
    t = np.arange(sr * 2) / sr
    audio = np.stack([np.sin(2 * np.pi * 120 * t) + 0.2 * np.sin(2 * np.pi * 4000 * t)] * 2, axis=1).astype(np.float32)
    path = tmp_path / "input.wav"
    sf.write(path, audio, sr, subtype="PCM_16")
    return path


def test_bandpass_frontend_is_aligned_and_deterministic(tmp_path: Path) -> None:
    source = _source(tmp_path)
    recipe = {"stages": [{"type": "bandpass", "low_hz": 70, "high_hz": 600, "order": 4}]}
    first = tmp_path / "one" / "analysis.wav"
    second = tmp_path / "two" / "analysis.wav"
    first_meta = render(source, first, recipe)
    second_meta = render(source, second, recipe)
    original = sf.info(source)
    output = sf.info(first)
    assert output.samplerate == original.samplerate
    assert output.frames == original.frames
    assert output.channels == original.channels
    assert first_meta["sha256"] == second_meta["sha256"]


def test_recipe_rejects_unknown_or_reordered_stages() -> None:
    try:
        canonical_recipe({"stages": [{"type": "soft_gate", "threshold_dbfs": -45, "reduction_db": 12, "attack_ms": 5, "release_ms": 80}, {"type": "bandpass", "low_hz": 30, "high_hz": 500}]})
    except AudioFrontendError:
        pass
    else:
        raise AssertionError("expected invalid order")


def test_frontend_hash_moves_when_recipe_changes() -> None:
    raw = "a" * 64
    assert frontend_hash(raw, {"stages": []}) != frontend_hash(raw, {"stages": [{"type": "bandpass", "low_hz": 30, "high_hz": 500}]})
