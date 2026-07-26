"""Focused, offline tests for optional key-detection backend routing."""

from pathlib import Path
import sys
import types

import pytest

from vgt import key


FIXTURE = Path(__file__).parents[1] / "test" / "Reaper Project" / "Media" / "The Seven Rivers (Full March - 3_00).mp3"


def _install_fake_essentia(monkeypatch: pytest.MonkeyPatch, *, loader: object, extractor: object) -> None:
    """Install just enough of Essentia's module shape for its local import."""
    essentia = types.ModuleType("essentia")
    standard = types.ModuleType("essentia.standard")
    standard.MonoLoader = loader  # type: ignore[attr-defined]
    standard.KeyExtractor = extractor  # type: ignore[attr-defined]
    essentia.standard = standard  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "essentia", essentia)
    monkeypatch.setitem(sys.modules, "essentia.standard", standard)


def test_detect_key_reports_a_missing_source(tmp_path: Path) -> None:
    source = tmp_path / "missing.mp3"

    with pytest.raises(key.KeyDetectionError, match="Reference source file not found"):
        key.detect_key(source)


def test_detect_key_falls_back_when_importable_essentia_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingLoader:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self) -> object:
            raise RuntimeError("broken Essentia decoder")

    _install_fake_essentia(monkeypatch, loader=FailingLoader, extractor=lambda: pytest.fail("extractor must not run"))
    monkeypatch.setattr(key, "_librosa_key", lambda _source: ("D", "minor", 0.6))
    source = tmp_path / "source.mp3"
    source.touch()

    assert key.detect_key(source) == {"root": "D", "scale": "minor", "confidence": 0.6, "backend": "librosa"}


def test_detect_key_reports_an_actionable_error_when_all_backends_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp3"
    source.touch()
    monkeypatch.setattr(key, "_essentia_key", lambda _source: None)

    def failing_librosa(_source: Path) -> tuple[str, str, float]:
        raise RuntimeError("decoder unavailable")

    monkeypatch.setattr(key, "_librosa_key", failing_librosa)

    with pytest.raises(key.KeyDetectionError, match="librosa fallback failed"):
        key.detect_key(source)


def test_detect_key_falls_back_when_essentia_returns_malformed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Loader:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self) -> object:
            return object()

    class Extractor:
        def __call__(self, _audio: object) -> tuple[str, str, float]:
            return "not-a-pitch-class", "major", 0.5

    _install_fake_essentia(monkeypatch, loader=Loader, extractor=Extractor)
    monkeypatch.setattr(key, "_librosa_key", lambda _source: ("E", "major", 0.4))
    source = tmp_path / "source.mp3"
    source.touch()

    assert key.detect_key(source)["backend"] == "librosa"


def test_detect_key_uses_a_valid_essentia_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Loader:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self) -> object:
            return object()

    class Extractor:
        def __call__(self, _audio: object) -> tuple[str, str, float]:
            return "A", "minor", 0.875

    _install_fake_essentia(monkeypatch, loader=Loader, extractor=Extractor)
    monkeypatch.setattr(key, "_librosa_key", lambda _source: pytest.fail("librosa must not run"))
    source = tmp_path / "source.mp3"
    source.touch()

    assert key.detect_key(source) == {"root": "A", "scale": "minor", "confidence": 0.875, "backend": "essentia"}


def test_detect_key_uses_librosa_for_the_real_local_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(key, "_essentia_key", lambda _source: None)

    result = key.detect_key(FIXTURE)

    assert result["backend"] == "librosa"
    assert result["root"] in key._PITCH_CLASSES
    assert result["scale"] in {"major", "minor"}
    assert 0.0 <= result["confidence"] <= 1.0
