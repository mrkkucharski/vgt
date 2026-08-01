"""Section boundary + label detection: MSAF's novelty/structure segmentation
is the primary backend, gated behind an optional `msaf`
extra/import since its last release predates modern Python/NumPy/SciPy and
its maintenance is uncertain -- exactly the fragility the issue calls out.

The always-available fallback implements the same family of technique by
hand rather than depending on a second heavy library: a self-similarity
novelty curve (Foote 2000, checkerboard-kernel convolution along the
diagonal of a chroma+MFCC similarity matrix), boundaries picked as novelty
peaks, and segments agglomeratively matched against earlier segments'
feature centroids so repeated sections (e.g. a chorus recurring) get the
same generic "A"/"B"/... label instead of a fresh one each time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .sidecar import artifact_namespace_dir

# Region boundaries describe changes in the character of the music, rather
# than every phrase-scale fluctuation. Pool the frame-level descriptors before
# computing novelty so the checkerboard operates at that structural timescale.
_ANALYSIS_FRAME_RATE_HZ = 2.0
_KERNEL_CONTEXT_SECONDS = 8.0
_PEAK_LOCAL_WINDOW_SECONDS = 4.0
_PEAK_DELTA = 0.10
# After local candidates are found, retain the strongest set whose neighboring
# boundaries are at least this far apart. This is separate from peak detection:
# using one large window for both used to let an early weak peak suppress a
# later, stronger section change.
_MIN_SECTION_SECONDS = 15.0
# Cosine similarity above which a segment is folded into an existing label
# cluster rather than starting a new one.
_LABEL_SIMILARITY_THRESHOLD = 0.85
_LABEL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class SectionDetectionError(RuntimeError):
    """No backend could produce a section list for the source audio."""


def _msaf_sections(source: Path) -> tuple[list[float], list[str]] | None:
    """Boundary times + generic labels via MSAF, or None if it isn't
    installed, fails to import, or raises during processing."""
    try:
        import msaf  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        boundaries, labels = msaf.process(str(source), boundaries_id="foote", labels_id="fmc2d")
    except Exception:
        return None
    boundary_times = [float(t) for t in boundaries]
    label_names = [str(label) for label in labels]
    if len(boundary_times) < 2 or len(label_names) != len(boundary_times) - 1:
        return None
    return boundary_times, label_names


def _checkerboard_kernel(half_width: int) -> Any:
    import numpy as np

    axis = np.arange(-half_width, half_width + 1)
    gaussian = np.exp(-0.5 * (axis / (half_width / 2)) ** 2)
    kernel = np.outer(gaussian, gaussian)
    sign = np.sign(np.outer(axis, axis))
    sign[sign == 0] = 1.0
    return kernel * sign


def _novelty_curve(features: Any, half_width: int) -> Any:
    """Foote's checkerboard-kernel novelty curve over the self-similarity
    matrix of `features` (n_features, n_frames)."""
    import warnings

    import numpy as np

    normed = features / (np.linalg.norm(features, axis=0, keepdims=True) + 1e-9)
    # numpy's float32 matmul SIMD kernel spuriously sets divide/overflow/invalid
    # floating-point flags from tail lanes past the valid data, even though every
    # input is finite and the result is correct (verified against a float64
    # computation to within float32 epsilon). Suppress only these known-false
    # matmul warnings; real numeric problems elsewhere still surface.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*encountered in matmul", category=RuntimeWarning)
        similarity = normed.T @ normed
    kernel = _checkerboard_kernel(half_width)
    padded = np.pad(similarity, half_width, mode="edge")
    n_frames = similarity.shape[0]
    novelty = np.zeros(n_frames)
    for i in range(n_frames):
        window = padded[i : i + 2 * half_width + 1, i : i + 2 * half_width + 1]
        novelty[i] = float(np.sum(window * kernel))
    novelty -= novelty.min()
    peak = novelty.max()
    if peak > 0:
        novelty /= peak
    return novelty


def _pick_boundaries(
    novelty: Any,
    frame_times: Any,
    duration: float,
    min_gap_seconds: float,
    *,
    peak_window_seconds: float = _PEAK_LOCAL_WINDOW_SECONDS,
    delta: float = _PEAK_DELTA,
) -> list[float]:
    import librosa
    import numpy as np

    hop = float(frame_times[1] - frame_times[0]) if len(frame_times) > 1 else 1.0
    window = max(1, int(round(peak_window_seconds / hop)))
    peak_frames = librosa.util.peak_pick(
        novelty,
        pre_max=window,
        post_max=window,
        pre_avg=window,
        post_avg=window,
        delta=delta,
        wait=window,
    )
    # Non-maximum suppression is strength-first. Chronologically accepting the
    # first candidate made weak phrase changes hide stronger nearby structural
    # changes for the full minimum-gap window.
    ranked_frames = sorted((int(frame) for frame in peak_frames), key=lambda frame: -novelty[frame])
    retained: list[float] = []
    for frame in ranked_frames:
        candidate = float(frame_times[frame])
        if candidate < min_gap_seconds or duration - candidate < min_gap_seconds:
            continue
        if all(abs(candidate - other) >= min_gap_seconds for other in retained):
            retained.append(candidate)
    return [0.0, *sorted(retained), duration]


def _pool_features(features: Any, frame_times: Any, sample_rate: int, hop_length: int, frame_rate: float) -> tuple[Any, Any]:
    """Mean-pool frame descriptors onto a stable, sample-rate-independent
    structural grid."""
    import numpy as np

    pool_width = max(1, int(round(sample_rate / (hop_length * frame_rate))))
    pooled_features = []
    pooled_times = []
    for start in range(0, features.shape[1], pool_width):
        stop = min(features.shape[1], start + pool_width)
        pooled_features.append(features[:, start:stop].mean(axis=1))
        pooled_times.append(float(frame_times[start:stop].mean()))
    return np.asarray(pooled_features).T, np.asarray(pooled_times)


def _label_segments(features: Any, frame_times: Any, boundaries: list[float], threshold: float) -> list[str]:
    import numpy as np

    frame_times = np.asarray(frame_times)
    centroids: list[Any] = []
    counts: list[int] = []
    labels: list[str] = []

    for start, end in zip(boundaries[:-1], boundaries[1:]):
        mask = (frame_times >= start) & (frame_times < end)
        if not mask.any():
            mask = np.zeros_like(frame_times, dtype=bool)
            mask[-1] = True
        vector = features[:, mask].mean(axis=1)
        norm_vector = vector / (np.linalg.norm(vector) + 1e-9)

        best_index, best_similarity = -1, -1.0
        for index, centroid in enumerate(centroids):
            norm_centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
            similarity = float(np.dot(norm_vector, norm_centroid))
            if similarity > best_similarity:
                best_index, best_similarity = index, similarity

        if best_index >= 0 and best_similarity >= threshold:
            count = counts[best_index]
            centroids[best_index] = (centroids[best_index] * count + vector) / (count + 1)
            counts[best_index] += 1
            labels.append(_LABEL_ALPHABET[best_index % len(_LABEL_ALPHABET)])
        else:
            centroids.append(vector)
            counts.append(1)
            labels.append(_LABEL_ALPHABET[(len(centroids) - 1) % len(_LABEL_ALPHABET)])

    return labels


def _librosa_sections(source: Path, settings: dict[str, Any]) -> tuple[list[float], list[str]]:
    import librosa
    import numpy as np

    min_section_seconds = float(settings.get("min_section_seconds", _MIN_SECTION_SECONDS))
    label_similarity_threshold = float(settings.get("label_similarity_threshold", _LABEL_SIMILARITY_THRESHOLD))

    y, sr = librosa.load(str(source), sr=None, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))
    hop_length = 512
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop_length)
    features = np.vstack([chroma, mfcc])
    # MFCC coefficients (especially c0, roughly log-energy) have far larger
    # and more varied scale than bounded chroma bins, which would otherwise
    # dominate the cosine similarities below and wash out timbral/harmonic
    # contrast between sections. Z-score each feature dimension so chroma and
    # MFCC contribute comparably.
    features = (features - features.mean(axis=1, keepdims=True)) / (features.std(axis=1, keepdims=True) + 1e-9)
    frame_times = librosa.frames_to_time(np.arange(features.shape[1]), sr=sr, hop_length=hop_length)
    features, frame_times = _pool_features(
        features, frame_times, sr, hop_length, _ANALYSIS_FRAME_RATE_HZ
    )

    kernel_half_width = max(1, round(_KERNEL_CONTEXT_SECONDS * _ANALYSIS_FRAME_RATE_HZ))
    novelty = _novelty_curve(features, kernel_half_width)
    boundaries = _pick_boundaries(novelty, frame_times, duration, min_section_seconds)
    labels = _label_segments(features, frame_times, boundaries, label_similarity_threshold)
    return boundaries, labels


def detect_sections(source: Path, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return a list of `{"index", "start_seconds", "end_seconds", "label",
    "backend"}` segments spanning `source` end-to-end."""
    settings = settings or {}
    if not source.is_file():
        raise SectionDetectionError(f"Reference source file not found: {source}")

    msaf_result = _msaf_sections(source)
    if msaf_result is not None:
        boundary_times, labels = msaf_result
        backend = "msaf"
    else:
        try:
            boundary_times, labels = _librosa_sections(source, settings)
        except Exception as exc:
            raise SectionDetectionError(
                f"Could not detect sections for {source}: MSAF was unavailable or failed, "
                f"and the librosa fallback failed ({exc})."
            ) from exc
        backend = "librosa"

    sections = []
    for index, (start, end, label) in enumerate(zip(boundary_times[:-1], boundary_times[1:], labels)):
        sections.append(
            {
                "index": index,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "label": label,
                "backend": backend,
            }
        )
    return sections


def section_timeline_path(project_path: Path, namespace: str) -> Path:
    """Verification artifact lives under the project's `vgt/<namespace>/`
    folder, not inside its own Media folder -- it is vgt-owned, regenerable,
    not part of the song."""
    return artifact_namespace_dir(project_path, namespace) / "sections.txt"


def _format_timestamp(seconds: float) -> str:
    minutes, remainder = divmod(max(seconds, 0.0), 60)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def render_section_timeline(sections: list[dict[str, Any]], destination: Path) -> Path:
    """Write a plain-text section timeline (timestamp range + label per
    section) for by-eye verification of the detected structure."""
    backend = sections[0]["backend"] if sections else "none"
    lines = [f"# backend: {backend}", ""]
    for section in sections:
        start = _format_timestamp(section["start_seconds"])
        end = _format_timestamp(section["end_seconds"])
        lines.append(f"{start} - {end}  {section['label']}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination
