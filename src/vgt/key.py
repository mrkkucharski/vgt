"""Key (root + scale) detection: Essentia's `KeyExtractor` is the primary
backend (optional -- Essentia wheels are fragile on Apple Silicon, so it is
never a hard dependency); a librosa chroma +
Krumhansl-Schmuckler template-correlation fallback is always available.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl & Kessler (1982) key profiles.
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


class KeyDetectionError(RuntimeError):
    """No backend could produce a key estimate for the source audio."""


def _validate_key_result(result: Any) -> tuple[str, str, float] | None:
    """Return a usable backend result, or ``None`` for malformed output."""
    try:
        if not isinstance(result, tuple) or len(result) != 3:
            return None
        root, scale, confidence = result
        if root not in _PITCH_CLASSES or scale not in ("major", "minor"):
            return None
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return None
    except Exception:
        return None
    return root, scale, confidence


def _essentia_key(source: Path) -> tuple[str, str, float] | None:
    """Root/scale/confidence via Essentia, or ``None`` when it is unusable.

    Essentia is optional and its Apple Silicon builds can import successfully
    but fail while decoding audio or executing ``KeyExtractor``. Treat either
    case, and malformed backend output, like an unavailable installation so
    the required librosa fallback remains reachable.
    """
    try:
        import essentia.standard as es  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        audio = es.MonoLoader(filename=str(source))()
        return _validate_key_result(es.KeyExtractor()(audio))
    except Exception:
        return None


def _correlate(profile_a, profile_b) -> float:
    import numpy as np

    a = np.asarray(profile_a, dtype=float)
    b = np.asarray(profile_b, dtype=float)
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


def _librosa_key(source: Path) -> tuple[str, str, float]:
    """Fallback: average chroma across the whole track, correlated against
    all 24 major/minor Krumhansl-Schmuckler templates. Confidence is the best
    correlation mapped from [-1, 1] onto [0, 1]."""
    import librosa
    import numpy as np

    y, sr = librosa.load(str(source), sr=None, mono=True)
    chroma_mean = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)

    scores: list[tuple[float, str, str]] = []
    for shift in range(12):
        major_template = np.roll(_MAJOR_PROFILE, shift)
        minor_template = np.roll(_MINOR_PROFILE, shift)
        scores.append((_correlate(chroma_mean, major_template), _PITCH_CLASSES[shift], "major"))
        scores.append((_correlate(chroma_mean, minor_template), _PITCH_CLASSES[shift], "minor"))

    scores.sort(key=lambda item: item[0], reverse=True)
    best_score, root, scale = scores[0]
    confidence = max(0.0, min(1.0, (best_score + 1.0) / 2.0))
    return root, scale, confidence


def detect_key(source: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return `{"root", "scale", "confidence", "backend"}` for `source`."""
    del settings  # no tunables yet; accepted for a uniform detector signature
    if not source.is_file():
        raise KeyDetectionError(f"Reference source file not found: {source}")

    essentia_result = _essentia_key(source)
    if essentia_result is not None:
        root, scale, confidence = essentia_result
        backend = "essentia"
    else:
        try:
            librosa_result = _librosa_key(source)
        except Exception as exc:
            raise KeyDetectionError(
                f"Could not detect key for {source}: Essentia was unavailable or failed, "
                f"and the librosa fallback failed ({exc}). Check that the source is readable "
                "and that vgt's audio dependencies are installed."
            ) from exc
        validated = _validate_key_result(librosa_result)
        if validated is None:
            raise KeyDetectionError(
                f"Could not detect key for {source}: Essentia was unavailable or failed, "
                "and the librosa fallback returned an invalid result."
            )
        root, scale, confidence = validated
        backend = "librosa"

    return {
        "root": root,
        "scale": scale,
        "confidence": round(confidence, 3),
        "backend": backend,
    }
