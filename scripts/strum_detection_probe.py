"""Measure classical strum-onset detectors against a human timing reference.

Evaluation-only: this script reads local audio and a numbers-only hand
annotation, writes no vgt state, and does not create a transcription variant.
It reports one-to-one onset precision, recall, and F1 at the shared 50 ms
tolerance, so dense false positives in a sustained guitar texture cannot hide
behind recall.

    uv run python scripts/strum_detection_probe.py \
      test/7Rivers/vgt/6a7745be/stems/guitar.wav

The default reference is the committed 7Rivers annotation. Pass ``--output``
to preserve a machine-readable JSON report for a findings document.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import librosa
import numpy as np

from vgt.drum_evaluation import ONSET_TOLERANCE_SECONDS, evaluate_instruments


ROOT = Path(__file__).parents[1]
DEFAULT_REFERENCE = ROOT / "tests" / "fixtures" / "strum_7rivers" / "hand_annotated_onsets.json"
HOP_LENGTH = 512
MINIMUM_INTER_ONSET_SECONDS = 0.080


def load_reference(path: Path) -> tuple[tuple[float, float], list[float]]:
    """Load and validate a numbers-only, audio-relative onset reference."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: reference must be an object")
    window = value.get("window_s")
    onsets = value.get("onsets_s")
    if (
        not isinstance(window, list)
        or len(window) != 2
        or not all(isinstance(item, (int, float)) for item in window)
        or not isinstance(onsets, list)
        or not all(isinstance(item, (int, float)) for item in onsets)
    ):
        raise ValueError(f"{path}: expected numeric window_s and onsets_s")
    low, high = float(window[0]), float(window[1])
    times = [float(item) for item in onsets]
    if low >= high or times != sorted(times) or any(time < low or time > high for time in times):
        raise ValueError(f"{path}: window/onset times are invalid")
    return (low, high), times


def _complex_domain_envelope(y: np.ndarray, *, hop_length: int = HOP_LENGTH) -> np.ndarray:
    """Complex-domain novelty: deviation from the preceding phase trajectory."""
    spectrum = librosa.stft(y, hop_length=hop_length)
    if spectrum.shape[1] < 3:
        return np.zeros(spectrum.shape[1], dtype=float)
    predicted_phase = 2 * np.angle(spectrum[:, 1:-1]) - np.angle(spectrum[:, :-2])
    predicted = np.abs(spectrum[:, 1:-1]) * np.exp(1j * predicted_phase)
    novelty = np.abs(spectrum[:, 2:] - predicted).sum(axis=0)
    return np.pad(novelty, (2, 0))


def detector_envelopes(y: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """Return the four explicitly named, classical detector candidates."""
    _, percussive = librosa.effects.hpss(y)
    return {
        "spectral-flux": librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP_LENGTH, lag=1, max_size=1),
        "superflux": librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP_LENGTH, lag=2, max_size=3),
        "complex-domain": _complex_domain_envelope(y),
        "hpss-percussive-flux": librosa.onset.onset_strength(y=percussive, sr=sr, hop_length=HOP_LENGTH, lag=1, max_size=1),
    }


def pick_onsets(envelope: np.ndarray, sr: int) -> list[float]:
    """Pick onset peaks with a fixed, reference-independent refractory period."""
    frames = librosa.onset.onset_detect(
        onset_envelope=envelope, sr=sr, hop_length=HOP_LENGTH, units="frames",
        pre_max=3, post_max=3, pre_avg=3, post_avg=5, delta=0.07,
        wait=round(MINIMUM_INTER_ONSET_SECONDS * sr / HOP_LENGTH),
    )
    return librosa.frames_to_time(frames, sr=sr, hop_length=HOP_LENGTH).tolist()


def in_window(times: list[float], window: tuple[float, float]) -> list[float]:
    low, high = window
    return [time for time in times if low <= time <= high]


def score_predictions(reference: list[float], predicted: list[float]) -> dict[str, object]:
    """Score the one unlabeled event stream with the shared exact matcher."""
    return evaluate_instruments(
        {"strum": reference}, {"strum": predicted}, tolerance=ONSET_TOLERANCE_SECONDS,
    )["global"]


def measure(audio: Path, reference_path: Path = DEFAULT_REFERENCE) -> dict[str, object]:
    window, reference = load_reference(reference_path)
    y, sr = librosa.load(audio, sr=None, mono=True)
    if sr <= 0:
        raise ValueError(f"{audio}: invalid sample rate")
    candidates: dict[str, dict[str, object]] = {}
    for name, envelope in detector_envelopes(y, sr).items():
        predicted = in_window(pick_onsets(envelope, sr), window)
        candidates[name] = {
            "onsets_s": predicted,
            "metrics": score_predictions(reference, predicted),
        }
    return {
        "kind": "offline-strum-detection-probe",
        "audio": str(audio),
        "reference": str(reference_path),
        "window_s": list(window),
        "reference_onset_count": len(reference),
        "tolerance_seconds": ONSET_TOLERANCE_SECONDS,
        "settings": {
            "hop_length": HOP_LENGTH,
            "minimum_inter_onset_seconds": MINIMUM_INTER_ONSET_SECONDS,
            "peak_delta": 0.07,
        },
        "candidates": candidates,
    }


def _summary(report: dict[str, object]) -> str:
    lines = ["detector                 pred  tp  fp  fn  precision  recall  f1"]
    candidates = report["candidates"]
    assert isinstance(candidates, dict)
    for name, candidate in candidates.items():
        assert isinstance(candidate, dict)
        onsets = candidate["onsets_s"]
        metrics = candidate["metrics"]
        assert isinstance(onsets, list) and isinstance(metrics, dict)
        lines.append(
            f"{name:<24} {len(onsets):>4} {metrics['true_positives']:>3} {metrics['false_positives']:>3} "
            f"{metrics['false_negatives']:>3} {metrics['precision']:>10.3f} {metrics['recall']:>7.3f} {metrics['f1']:>5.3f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="local guitar stem WAV/audio file")
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE, help="numbers-only hand onset annotation")
    parser.add_argument("--output", type=Path, help="write deterministic JSON report")
    args = parser.parse_args(argv)
    report = measure(args.audio, args.reference)
    print(_summary(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
