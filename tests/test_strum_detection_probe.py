"""Offline regression tests for the Phase 1 strum-detector evaluation probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest


PROBE_PATH = Path(__file__).parents[1] / "scripts" / "strum_detection_probe.py"
REFERENCE = Path(__file__).parent / "fixtures" / "strum_7rivers" / "hand_annotated_onsets.json"


def _probe_module():
    spec = importlib.util.spec_from_file_location("strum_detection_probe_test", PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reference_is_a_continuous_20_to_30_second_human_window() -> None:
    probe = _probe_module()
    window, onsets = probe.load_reference(REFERENCE)
    assert 20 <= window[1] - window[0] <= 30
    assert len(onsets) == 30
    assert window[0] <= onsets[0] < onsets[-1] <= window[1]


def test_reference_rejects_unsorted_or_out_of_window_onsets(tmp_path: Path) -> None:
    probe = _probe_module()
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"window_s": [1, 2], "onsets_s": [1.5, 1.2]}))
    with pytest.raises(ValueError, match="invalid"):
        probe.load_reference(path)


def test_score_makes_extra_candidates_reduce_precision() -> None:
    probe = _probe_module()
    metrics = probe.score_predictions([1.0, 2.0], [1.01, 1.5, 2.01])
    assert metrics == {
        "true_positives": 2, "false_positives": 1, "false_negatives": 0,
        "precision": 2 / 3, "recall": 1.0, "f1": 0.8,
    }


def test_complex_domain_envelope_tracks_stft_frames() -> None:
    probe = _probe_module()
    y = np.zeros(4096, dtype=float)
    y[1024] = 1.0
    envelope = probe._complex_domain_envelope(y)
    assert len(envelope) == 1 + len(y) // probe.HOP_LENGTH
    assert np.isfinite(envelope).all()


def test_window_excludes_predictions_outside_annotation() -> None:
    probe = _probe_module()
    assert probe.in_window([0.9, 1.0, 1.5, 2.0, 2.1], (1.0, 2.0)) == [1.0, 1.5, 2.0]
