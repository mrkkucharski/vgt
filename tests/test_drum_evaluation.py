from __future__ import annotations

import json
from pathlib import Path
import runpy

from vgt.drum_evaluation import collapse_basic_pitch_starts, evaluate_instruments, instrument_onsets, shadow_comparison


def test_annotated_metrics_are_per_class_with_macro_and_global_context() -> None:
    report = evaluate_instruments(
        {"kick": [0.0, 1.0], "crash": [0.5]},
        {"kick": [0.03, 1.2], "crash": [], "ride": [0.5]},
    )
    assert report["per_instrument"]["kick"]["true_positives"] == 1
    assert report["per_instrument"]["kick"]["f1"] == 0.5
    assert report["per_instrument"]["crash"]["f1"] == 0.0
    assert report["per_instrument"]["ride"]["false_positives"] == 1
    assert report["macro"]["f1"] < report["global"]["f1"]


def test_instrument_onsets_preserves_multi_instrument_events() -> None:
    assert instrument_onsets([{"time_sec": 0.2, "instruments": ["kick", "snare"]}]) == {"kick": [0.2], "snare": [0.2]}


def test_shadow_clusters_unlabeled_basic_pitch_starts_and_counts_each_side() -> None:
    assert collapse_basic_pitch_starts([0.001, 0.015, 0.20]) == [0.001, 0.2]
    report = shadow_comparison(
        [{"time_sec": 0.0, "instruments": ["kick", "snare"]}, {"time_sec": 0.4, "instruments": ["crash"]}],
        [0.01, 0.015, 0.8],
    )
    assert report == {
        "kind": "temporary-evaluation-only-shadow-comparison", "tolerance_seconds": 0.05,
        "cluster_window_seconds": 0.02, "drumscript_onsets": 2, "basic_pitch_transient_clusters": 2,
        "matched": 1, "drumscript_only": 1, "basic_pitch_only": 1,
    }


def test_checked_in_annotations_and_events_produce_the_documented_report() -> None:
    root = Path(__file__).parent / "fixtures" / "drum_evaluation"
    manifest = json.loads((root / "annotated-manifest.json").read_text(encoding="utf-8"))
    annotations = manifest["clips"][0]["annotations"]
    events = json.loads((root / "events" / "pattern-a.json").read_text(encoding="utf-8"))
    report = evaluate_instruments(annotations, instrument_onsets(events))
    assert report["global"]["f1"] == 0.5714285714285715
    assert report["per_instrument"]["crash"]["false_negatives"] == 1


def test_idmt_annotation_parser_maps_only_the_official_three_classes(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(Path("scripts/idmt_drum_manifest.py")))
    annotation = tmp_path / "clip.xml"
    annotation.write_text("<instrumentRecording><transcription>"
                          "<event><instrument>KD</instrument><onsetSec>0.1</onsetSec></event>"
                          "<event><instrument>SD</instrument><onsetSec>0.2</onsetSec></event>"
                          "<event><instrument>HH</instrument><onsetSec>0.3</onsetSec></event>"
                          "</transcription></instrumentRecording>", encoding="utf-8")
    assert namespace["parse_annotation"](annotation) == {"kick": [0.1], "snare": [0.2], "hi_hat_closed": [0.3]}
