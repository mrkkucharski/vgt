"""vgt's REAPER project plumbing and reference-track analysis."""

from .project import ProjectInfo, TrackInfo, locate_project, read_project

__version__ = "0.1.0"

__all__ = ["ProjectInfo", "TrackInfo", "locate_project", "read_project", "__version__"]
