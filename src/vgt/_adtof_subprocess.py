"""Pinned ADTOF inference entry point, executed only in the isolated runner.

This module is deliberately not imported by vgt.  ``AdtofActivationRunner``
copies it into its temporary work directory and invokes it with the pinned
environment, where Torch and ADTOF are installed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

import numpy as np
import torch
from adtof_pytorch import LABELS_5, get_default_weights_path, transcribe_to_midi


CLASS_NAMES = ["bass_drum", "snare_drum", "tom_tom", "hi_hat", "cymbal"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str]) -> None:
    if len(argv) != 4:
        raise SystemExit("usage: _adtof_subprocess.py INPUT.wav OUTPUT.npz LOCK_SHA256")
    source, output = (Path(value).resolve() for value in argv[1:3])
    if not source.is_file():
        raise SystemExit("input drum stem is not a readable file")

    # The port itself puts its model in eval mode and uses no_grad for this
    # call.  Fixing the CPU seed here makes that contract explicit at vgt's
    # boundary too, without enabling any accelerator execution.
    torch.manual_seed(0)
    raw = transcribe_to_midi(source, output.with_suffix(".unused.mid"), device="cpu", return_activations=True)
    activations = np.asarray(raw[0], dtype=np.float32)
    if activations.ndim != 2:
        raise RuntimeError(f"unexpected ADTOF activation shape {activations.shape!r}")

    weights = Path(get_default_weights_path())
    if not weights.is_file():
        raise RuntimeError("pinned ADTOF checkpoint is missing")
    metadata = {
        "package_version": importlib.metadata.version("adtof-pytorch"),
        "model_version": "Frame_RNN",
        "weights_sha256": _sha256(weights),
        "runtime_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "torch_version": torch.__version__,
        "lock_sha256": argv[3],
        "device": "cpu",
        "sample_rate": 44100,
        "n_fft": 2048,
        "hop_samples": 441,
        "fps": 100,
        "class_names": CLASS_NAMES,
        "gm_labels": list(LABELS_5),
    }
    np.savez_compressed(output, activations=activations, metadata=json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    import sys

    main(sys.argv)
