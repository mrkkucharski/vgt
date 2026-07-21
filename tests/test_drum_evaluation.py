from __future__ import annotations

import json
from pathlib import Path
import runpy

from vgt.drum_evaluation import aggregate_instrument_metrics, collapse_basic_pitch_starts, evaluate_instruments, instrument_onsets, shadow_comparison


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


def test_onset_matching_maximizes_valid_pairs_before_minimizing_distance() -> None:
    # A nearest-first match would pair 0.04 with 0.05 and leave 0.08 unmatched.
    report = evaluate_instruments({"kick": [0.04, 0.08]}, {"kick": [0.0, 0.05]})
    assert report["per_instrument"]["kick"] == {
        "true_positives": 2, "false_positives": 0, "false_negatives": 0,
        "precision": 1.0, "recall": 1.0, "f1": 1.0,
    }


def test_aggregate_metrics_never_matches_onsets_from_different_clips() -> None:
    # Both clips use time zero, but their annotations are independent timelines.
    report = aggregate_instrument_metrics([
        evaluate_instruments({"kick": [0.0]}, {"kick": []}),
        evaluate_instruments({"kick": []}, {"kick": [0.0]}),
    ])
    assert report["per_instrument"]["kick"] == {
        "true_positives": 0, "false_positives": 1, "false_negatives": 1,
        "precision": 0.0, "recall": 0.0, "f1": 0.0,
    }


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


def test_benchmark_command_scores_checked_in_fixture(tmp_path: Path, capsys) -> None:
    root = Path(__file__).parent / "fixtures" / "drum_evaluation"
    main = runpy.run_path(str(Path("scripts/drumscript_benchmark.py")))["main"]
    assert main([str(root / "annotated-manifest.json"), "--events-dir", str(root / "events")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "annotated-drumscript-benchmark"
    assert report["manifest_sha256"]
    assert report["metrics"]["per_instrument"]["crash"]["f1"] == 0.0


def test_shadow_command_writes_temporary_unlabeled_report(tmp_path: Path) -> None:
    root = Path(__file__).parent / "fixtures" / "drum_evaluation"
    notes = tmp_path / "basic-pitch-notes.csv"
    notes.write_text(
        "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.01,0.1,36,100\n0.015,0.1,42,100\n0.8,0.9,60,100\n",
        encoding="utf-8",
    )
    output = tmp_path / "shadow.json"
    main = runpy.run_path(str(Path("scripts/drumscript_shadow_compare.py")))["main"]
    assert main([str(root / "events" / "pattern-a.json"), str(notes), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["kind"] == "temporary-evaluation-only-shadow-comparison"
    assert set(report) == {
        "kind", "tolerance_seconds", "cluster_window_seconds", "drumscript_onsets",
        "basic_pitch_transient_clusters", "matched", "drumscript_only", "basic_pitch_only",
    }


def test_shadow_command_rejects_production_artifact_paths(tmp_path: Path) -> None:
    root = Path(__file__).parent / "fixtures" / "drum_evaluation"
    notes = tmp_path / "basic-pitch-notes.csv"
    notes.write_text("start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\\n", encoding="utf-8")
    main = runpy.run_path(str(Path("scripts/drumscript_shadow_compare.py")))["main"]
    production_path = tmp_path / "vgt" / "namespace" / "transcription" / "drums.json"
    try:
        main([str(root / "events" / "pattern-a.json"), str(notes), "--output", str(production_path)])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("expected production path rejection")


def test_idmt_annotation_parser_maps_only_the_official_three_classes(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(Path("scripts/idmt_drum_manifest.py")))
    annotation = tmp_path / "clip.xml"
    annotation.write_text("<instrumentRecording><transcription>"
                          "<event><instrument>KD</instrument><onsetSec>0.1</onsetSec></event>"
                          "<event><instrument>SD</instrument><onsetSec>0.2</onsetSec></event>"
                          "<event><instrument>HH</instrument><onsetSec>0.3</onsetSec></event>"
                          "</transcription></instrumentRecording>", encoding="utf-8")
    assert namespace["parse_annotation"](annotation) == {"kick": [0.1], "snare": [0.2], "hi_hat_closed": [0.3]}
