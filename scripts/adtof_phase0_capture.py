#!/usr/bin/env python3
"""Reproduce the Phase 0 ADTOF raw-activation capture.

This is a feasibility-spike utility, not part of vgt's runtime.  Invoke it in
the isolated environment documented in docs/adtof-phase-0-feasibility-findings.md.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from adtof_pytorch import (
    FRAME_RNN_THRESHOLDS,
    LABELS_5,
    get_default_weights_path,
    transcribe_to_midi,
)


ENGINE_COMMIT = "85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"
ENGINE_URL = "https://github.com/xavriley/ADTOF-pytorch.git"
CLASS_NAMES = ["bass_drum", "snare_drum", "tom_tom", "hi_hat", "cymbal"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def count_local_peaks(activations: np.ndarray, thresholds: list[float]) -> list[int]:
    """Count simple per-class maxima solely as a capture sanity statistic."""
    counts: list[int] = []
    for column, threshold in zip(activations.T, thresholds, strict=True):
        local_max = (column[1:-1] >= column[:-2]) & (column[1:-1] > column[2:])
        counts.append(int(np.count_nonzero(local_max & (column[1:-1] >= threshold))))
    return counts


def run_once(audio: Path) -> tuple[np.ndarray, float]:
    start = time.perf_counter()
    output = transcribe_to_midi(
        audio,
        Path("/tmp/adtof-phase0-unused.mid"),
        device="cpu",
        return_activations=True,
    )
    return np.asarray(output[0], dtype=np.float32), time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("output_prefix", type=Path)
    args = parser.parse_args()

    audio = args.audio.resolve()
    output_prefix = args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    activations, first_runtime_s = run_once(audio)
    repeated, second_runtime_s = run_once(audio)
    np.savez_compressed(output_prefix.with_suffix(".npz"), activations=activations)

    info = sf.info(audio)
    weights = Path(get_default_weights_path())
    metadata = {
        "capture": "ADTOF Phase 0 feasibility spike; raw model activations before peak picking",
        "source": {
            "path": str(args.audio),
            "sha256": sha256_file(audio),
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "frames": info.frames,
            "duration_s": info.duration,
        },
        "engine": {
            "vcs_url": ENGINE_URL,
            "commit": ENGINE_COMMIT,
            "package_version": importlib.metadata.version("adtof-pytorch"),
            "torch_version": torch.__version__,
            "python_version": platform.python_version(),
            "device": "cpu",
            "weights_resource": "adtof_pytorch/data/adtof_frame_rnn_pytorch_weights.pth",
            "weights_sha256": sha256_file(weights),
        },
        "activation_contract": {
            "array_key": "activations",
            "shape": list(activations.shape),
            "dtype": str(activations.dtype),
            "sha256": hashlib.sha256(activations.tobytes()).hexdigest(),
            "model_sample_rate": 44100,
            "n_fft": 2048,
            "hop_samples": 441,
            "fps": 100,
            "center": True,
            "class_names": CLASS_NAMES,
            "gm_labels": list(LABELS_5),
            "upstream_peak_thresholds": list(FRAME_RNN_THRESHOLDS),
        },
        "measurement": {
            "first_run_s": first_runtime_s,
            "second_run_s": second_runtime_s,
            "second_run_max_abs_diff": float(np.max(np.abs(activations - repeated))),
            "second_run_bitwise_identical": bool(np.array_equal(activations, repeated)),
            "local_peak_counts_at_upstream_thresholds": count_local_peaks(
                activations, list(FRAME_RNN_THRESHOLDS)
            ),
        },
    }
    output_prefix.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
