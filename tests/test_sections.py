import builtins
from pathlib import Path
import sys
import types

import numpy as np
import pytest

from vgt.sections import (
    SectionDetectionError,
    _label_segments,
    _novelty_curve,
    detect_sections,
)


def _synthetic_features(block_vectors: list[list[float]], frames_per_block: int) -> np.ndarray:
    """Stack repeated `block_vectors` columns into a (n_features, n_frames)
    matrix, simulating a track with abruptly changing timbre/harmony."""
    columns = []
    for vector in block_vectors:
        columns.extend([vector] * frames_per_block)
    return np.asarray(columns, dtype=float).T


def test_novelty_curve_peaks_near_feature_change() -> None:
    features = _synthetic_features([[1.0, 0.0], [0.0, 1.0]], frames_per_block=40)
    novelty = _novelty_curve(features, half_width=8)

    boundary_frame = 40  # where block A ends and block B begins
    window = range(boundary_frame - 5, boundary_frame + 5)
    assert max(novelty[i] for i in window) == pytest.approx(novelty.max())


def test_label_segments_reuses_label_for_similar_recurring_segments() -> None:
    features = _synthetic_features([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]], frames_per_block=10)
    frame_times = np.arange(features.shape[1]) * 0.1
    boundaries = [0.0, 1.0, 2.0, 3.0]

    labels = _label_segments(features, frame_times, boundaries, threshold=0.9)

    assert labels[0] == labels[2]  # first and third segments share the A-like vector
    assert labels[0] != labels[1]


def test_label_segments_gives_distinct_labels_when_dissimilar() -> None:
    features = _synthetic_features([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]], frames_per_block=10)
    frame_times = np.arange(features.shape[1]) * 0.1
    boundaries = [0.0, 1.0, 2.0, 3.0]

    labels = _label_segments(features, frame_times, boundaries, threshold=0.99)

    assert len(set(labels)) == 3


def test_detect_sections_requires_an_existing_source_file(tmp_path: Path) -> None:
    with pytest.raises(SectionDetectionError, match="not found"):
        detect_sections(tmp_path / "missing.mp3")


def test_detect_sections_falls_back_when_msaf_initialization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incompatible MSAF import must leave the librosa path usable."""
    import vgt.sections as sections_module

    source = tmp_path / "source.mp3"
    source.touch()
    original_import = builtins.__import__

    def failing_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "msaf":
            raise RuntimeError("MSAF extension initialization failed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    monkeypatch.setattr(sections_module, "_librosa_sections", lambda _source, _settings: ([0.0, 2.0], ["A"]))

    assert detect_sections(source) == [
        {"index": 0, "start_seconds": 0.0, "end_seconds": 2.0, "label": "A", "backend": "librosa"}
    ]


def test_detect_sections_falls_back_when_msaf_processing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime failure remains equivalent to an unavailable MSAF backend."""
    import vgt.sections as sections_module

    def failing_process(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("MSAF processing failed")

    msaf_module = types.ModuleType("msaf")
    msaf_module.process = failing_process  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msaf", msaf_module)
    source = tmp_path / "source.mp3"
    source.touch()
    monkeypatch.setattr(sections_module, "_librosa_sections", lambda _source, _settings: ([0.0, 2.0], ["A"]))

    assert detect_sections(source)[0]["backend"] == "librosa"


def test_detect_sections_normalizes_a_terminal_fallback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vgt.sections as sections_module

    source = tmp_path / "source.mp3"
    source.touch()
    monkeypatch.setattr(sections_module, "_msaf_sections", lambda _source: None)

    def failing_librosa(_source: Path, _settings: dict[str, object]) -> tuple[list[float], list[str]]:
        raise RuntimeError("decoder unavailable")

    monkeypatch.setattr(sections_module, "_librosa_sections", failing_librosa)

    with pytest.raises(SectionDetectionError, match="librosa fallback failed"):
        detect_sections(source)


def test_detect_sections_on_real_fixture_covers_the_track_end_to_end() -> None:
    fixture = Path(__file__).parents[1] / "test" / "Reaper Project" / "Media" / "The Seven Rivers (Full March - 3_00).mp3"

    sections = detect_sections(fixture)

    assert len(sections) > 0
    assert sections[0]["start_seconds"] == 0.0
    for earlier, later in zip(sections, sections[1:]):
        assert earlier["end_seconds"] == later["start_seconds"]
        assert later["start_seconds"] > earlier["start_seconds"]
    for section in sections:
        assert section["backend"] in {"msaf", "librosa"}
