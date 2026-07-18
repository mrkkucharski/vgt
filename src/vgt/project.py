"""Read-only helpers for locating and inspecting REAPER RPP projects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re


class ProjectError(ValueError):
    """A project cannot be located or does not contain required RPP data."""


@dataclass(frozen=True)
class TrackInfo:
    name: str
    guid: str


@dataclass(frozen=True)
class ProjectInfo:
    path: Path
    sample_rate: int
    tempo: float
    time_signature_numerator: int
    time_signature_denominator: int
    tracks: tuple[TrackInfo, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["path"] = str(self.path)
        return result


_SAMPLERATE = re.compile(r"^\s*SAMPLERATE\s+(\d+)", re.MULTILINE)
_TEMPO = re.compile(r"^\s*TEMPO\s+([0-9.]+)\s+(\d+)\s+(\d+)", re.MULTILINE)
_TRACK_START = re.compile(r"^\s*<TRACK\s+({[^}]+})", re.MULTILINE)
_NAME = re.compile(r'^\s*NAME\s+(?:"(?P<quoted>.*)"|(?P<plain>\S.*))\s*$', re.MULTILINE)


def locate_project(project: str | Path | None, working_directory: Path | None = None) -> Path:
    """Resolve an explicit RPP path or the single RPP in the working directory."""
    if project is not None:
        path = Path(project).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != ".rpp":
            raise ProjectError(f"REAPER project not found: {path}")
        return path

    directory = (working_directory or Path.cwd()).resolve()
    candidates = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".rpp")
    if not candidates:
        raise ProjectError(f"No .RPP project found in {directory}; pass a project path explicitly.")
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ProjectError(f"More than one .RPP project found in {directory}: {names}. Pass one explicitly.")
    return candidates[0]


def _track_blocks(text: str) -> list[tuple[str, str]]:
    """Return each top-level track GUID and its RPP chunk without parsing nested chunks."""
    starts = list(_TRACK_START.finditer(text))
    blocks: list[tuple[str, str]] = []
    for start in starts:
        depth = 0
        for line_match in re.finditer(r".*(?:\n|$)", text[start.start() :]):
            line = line_match.group(0)
            stripped = line.strip()
            if stripped.startswith("<"):
                depth += 1
            elif stripped == ">":
                depth -= 1
                if depth == 0:
                    end = start.start() + line_match.end()
                    blocks.append((start.group(1), text[start.start() : end]))
                    break
        else:
            raise ProjectError(f"Malformed TRACK chunk for {start.group(1)}")
    return blocks


def read_project(path: str | Path) -> ProjectInfo:
    """Read Phase 0 metadata without changing the RPP file."""
    project_path = locate_project(path)
    text = project_path.read_text(encoding="utf-8", errors="replace")
    sample_rate = _SAMPLERATE.search(text)
    tempo = _TEMPO.search(text)
    if sample_rate is None or tempo is None:
        raise ProjectError(f"{project_path} does not contain SAMPLERATE and TEMPO settings")

    tracks = []
    for guid, block in _track_blocks(text):
        name_match = _NAME.search(block)
        tracks.append(TrackInfo(name=(name_match.group("quoted") or name_match.group("plain")) if name_match else "", guid=guid))
    return ProjectInfo(
        path=project_path,
        sample_rate=int(sample_rate.group(1)),
        tempo=float(tempo.group(1)),
        time_signature_numerator=int(tempo.group(2)),
        time_signature_denominator=int(tempo.group(3)),
        tracks=tuple(tracks),
    )
