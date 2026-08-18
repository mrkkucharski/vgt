"""Explicit, idempotent provisioning for the pinned MT3 backend (issue #285).

MT3 has a TensorFlow/JAX/T5X dependency graph that must never enter vgt's own
Python environment. This module only ever shells out to `git` and `uv` against
a repository/tag pinned below, and it never runs implicitly -- the only entry
point is `vgt transcription backend provision mt3`. Ordinary `vgt analyze`
execution must not import this module's provisioning path.

The pinned fork's own README documents the exact commands a host application
is expected to run; `_checkout_repo`, `_build_environment`, and
`_download_model` mirror them literally so a human can reproduce or debug a
failure by hand:

    git clone --branch v0.1.1 https://github.com/mrkkucharski/mt3.git /path/to/mt3
    uv sync --project /path/to/mt3 --frozen
    uv run --project /path/to/mt3 mt3-download-model --output-dir /path/to/models/mt3 --json
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Any

MT3_REPO_URL = "https://github.com/mrkkucharski/mt3.git"
# v0.1.0's own `mt3-download-model` had a bug: it wrote checkpoint files
# straight into `--output-dir` but then required an `output-dir/checkpoint_0`
# subdirectory that the real GCS bucket layout could never produce, so
# provisioning always failed at the download step. v0.1.1 fixed that (see
# https://github.com/mrkkucharski/mt3/commit/36c9d1bfdc8f432376c1ca303cb965e71ef297d6).
# `main` at this exact commit adds head-cropped, overlapping encoder windows
# to `mt3-transcribe` (the released v0.1.2 cannot run the 4 s / 50% overlap
# checkpoint approach), a `--force-program` flag that pins every decoded note
# onto one GM program instead of MT3's own program-change predictions, and
# (as of this pin) also makes `--force-program` suppress the rhythm/lead
# split it otherwise still predicts independently of program -- see
# `mt3/cli.py`, `mt3/transcription.py`, and `mt3/note_sequences.py` at this
# commit. `mt3_normalize.merge_all_musical_tracks` still merges every
# surviving non-drum track regardless, so this is a safety margin, not a
# behavior this pin depends on. The branch name is only the checkout ref;
# the commit check below is the reproducible version pin -- `main` is a
# mutable ref that moves forward independently of this file, so provisioning
# refuses outright (rather than silently using whatever main's tip currently
# is) whenever the checked-out commit disagrees with this pin.
MT3_PINNED_TAG = "main"
MT3_PINNED_COMMIT = "dbe0ffcecca88e0ae5c0c73e69cbaebfac85f142"

MT3_CACHE_DIR_ENV = "VGT_MT3_CACHE_DIR"

# From the pinned commit's pyproject.toml: `requires-python = ">=3.11,<3.12"`,
# `[tool.uv] required-version = ">=0.9.20,<0.10"`, and
# `environments = ["sys_platform == 'darwin' and platform_machine == 'arm64'"]`.
MT3_REQUIRED_PYTHON = (3, 11)
MT3_MIN_UV_VERSION = (0, 9, 20)
MT3_MAX_UV_VERSION_EXCLUSIVE = (0, 10, 0)
MT3_RUNTIME_VERSION = f"python=={MT3_REQUIRED_PYTHON[0]}.{MT3_REQUIRED_PYTHON[1]}"

# sha256 of the pinned commit's own `uv.lock` -- fully determined by
# `MT3_PINNED_COMMIT` (it is part of that commit's tree), so this is a
# reproducible identity constant rather than a value read from a local
# checkout. Verify it against the pinned commit with:
#   gh api "repos/mrkkucharski/mt3/contents/uv.lock?ref=<MT3_PINNED_COMMIT>" \
#     --jq '.content' | base64 -d | shasum -a 256
MT3_LOCK_SHA256 = "1f103f1395c42617a1df98ade4238a7bff8ce84c3e6f7bf3fd1c45e63bf04f46"

# The model downloader in the overlap-capable fork still defaults to the
# earlier 2 s checkpoint, so provisioning fetches this explicitly instead.
# Both the model repo revision and directory are pinned: `main` on Hugging
# Face is mutable and must not decide a transcription cache identity.
MT3_MODEL_ID = "guitar-pilot-70ex-checkpoint-1126000"
MT3_HF_REPO_ID = "mrkkucharski/mt3-guitar-pilot"
MT3_HF_REVISION = "93e070d82fd1e79b11b32059c53d6f31b4167564"
# Unlike the sibling checkpoint dirs at repo root (e.g. checkpoint_1108000/,
# with _CHECKPOINT_METADATA directly inside), this one was uploaded with an
# extra nested `checkpoint_1126000/` level -- verified against the live HF
# tree before pinning. The full path is required to land the same flat
# layout `_download_model` normalizes into `checkpoint_0`.
MT3_HF_CHECKPOINT_DIR = "70ex_checkpoint_1126000/checkpoint_1126000"

# 512 spectrogram frames at MT3's fixed 125 frames/s is a ~4.096 s window.
# This checkpoint (70ex_checkpoint_1126000) is pinned to run with *no*
# right-context lookahead/overlap -- unlike the earlier it3_4s checkpoint's
# 256-frame (50%) overlap, each window is decoded independently with a
# zero-frame hop overrun. These must match the checkpoint's own
# training/inference approach.
MT3_INPUT_LENGTH_FRAMES = 512
MT3_LOOKAHEAD_FRAMES = 0


class Mt3ProvisionError(Exception):
    """The pinned mt3 backend could not be provisioned or verified."""


@dataclass(frozen=True)
class RuntimeProbe:
    system: str
    machine: str
    ffmpeg_path: str | None
    uv_version: tuple[int, int, int] | None
    python311_available: bool


@dataclass(frozen=True)
class ModelFileRecord:
    relative_path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"relative_path": self.relative_path, "size_bytes": self.size_bytes, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelFileRecord":
        return cls(relative_path=str(data["relative_path"]), size_bytes=int(data["size_bytes"]), sha256=str(data["sha256"]))


@dataclass(frozen=True)
class CheckpointManifest:
    """Deterministic local fingerprint over the provisioned checkpoint's files.

    Later transcription backend code (out of scope here) uses `fingerprint`
    as part of its cache identity, the same way ADTOF's lock hash and Basic
    Pitch's package pin already are.
    """

    commit: str
    tag: str
    files: tuple[ModelFileRecord, ...]
    fingerprint: str
    model_id: str
    hf_revision: str
    hf_checkpoint_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit, "tag": self.tag, "files": [f.to_dict() for f in self.files], "fingerprint": self.fingerprint,
            "model_id": self.model_id, "hf_revision": self.hf_revision, "hf_checkpoint_dir": self.hf_checkpoint_dir,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CheckpointManifest":
        return cls(
            commit=str(data["commit"]), tag=str(data["tag"]),
            files=tuple(ModelFileRecord.from_dict(item) for item in data["files"]), fingerprint=str(data["fingerprint"]),
            model_id=str(data["model_id"]), hf_revision=str(data["hf_revision"]), hf_checkpoint_dir=str(data["hf_checkpoint_dir"]),
        )


@dataclass(frozen=True)
class Mt3ProvisionResult:
    cache_dir: Path
    repo_dir: Path
    model_dir: Path
    manifest: CheckpointManifest
    already_provisioned: bool


def default_cache_dir() -> Path:
    """The user cache root, outside any REAPER project and vgt sidecar."""
    override = os.environ.get(MT3_CACHE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Caches" / "vgt" / "mt3"


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _uv_version(uv_path: str) -> tuple[int, int, int] | None:
    try:
        completed = subprocess.run([uv_path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _python311_available(uv_path: str | None) -> bool:
    if shutil.which("python3.11"):
        return True
    if uv_path is None:
        return False
    try:
        completed = subprocess.run([uv_path, "python", "find", "3.11"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_uv_executable() -> str:
    """Absolute path to `uv`, falling back to common install locations when
    it isn't on `PATH`.

    A normal terminal-invoked `vgt` command inherits the user's shell `PATH`,
    which has always included wherever `uv` was installed, so a bare `"uv"`
    has worked for every subprocess call here until now. The on-demand
    track-transcription job runner (docs/on-demand-track-transcription-plan.md)
    changed that: it is spawned by a REAPER ReaScript action, and a macOS GUI
    app does not inherit a login shell's environment -- REAPER's own `PATH`
    is typically just `/usr/bin:/bin:/usr/sbin:/sbin`, missing `~/.local/bin`
    or `~/.cargo/bin` entirely (the same trap the plan already solved for
    locating vgt's own interpreter via the sidecar `runtime` block; `uv` is a
    second, separate instance of the identical problem, one level deeper).
    Never used by `probe_runtime`, which must keep reporting a plain `PATH`
    miss so `diagnose_runtime` still tells an interactive terminal user to
    install `uv` rather than silently finding a fallback and hiding it.
    """
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv"):
        if candidate.is_file():
            return str(candidate)
    return "uv"  # last resort: subprocess raises its own clear "not found" error


def probe_runtime() -> RuntimeProbe:
    """Real, local-only environment probe (no network access)."""
    uv_path = shutil.which("uv")
    return RuntimeProbe(
        system=platform.system(), machine=platform.machine(), ffmpeg_path=shutil.which("ffmpeg"),
        uv_version=_uv_version(uv_path) if uv_path else None, python311_available=_python311_available(uv_path),
    )


def diagnose_runtime(probe: RuntimeProbe) -> tuple[str, ...]:
    """Actionable problems with `probe`, or an empty tuple if it can build mt3."""
    problems: list[str] = []
    if not (probe.system == "Darwin" and probe.machine in {"arm64", "aarch64"}):
        problems.append(
            "mt3 is pinned to Apple Silicon macOS (pyproject.toml tool.uv.environments); "
            f"detected {probe.system}/{probe.machine}"
        )
    if probe.ffmpeg_path is None:
        problems.append("ffmpeg was not found on PATH; install it with `brew install ffmpeg`")
    if probe.uv_version is None:
        problems.append("uv was not found on PATH; install it from https://docs.astral.sh/uv/")
    elif not (MT3_MIN_UV_VERSION <= probe.uv_version < MT3_MAX_UV_VERSION_EXCLUSIVE):
        problems.append(
            f"mt3's pyproject.toml pins {_format_version(MT3_MIN_UV_VERSION)} <= uv < "
            f"{_format_version(MT3_MAX_UV_VERSION_EXCLUSIVE)}; detected uv {_format_version(probe.uv_version)}"
        )
    if not probe.python311_available:
        problems.append(
            "no Python 3.11 toolchain is available for uv (mt3 requires-python is >=3.11,<3.12); "
            "run `uv python install 3.11`"
        )
    return tuple(problems)


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def _tail(*streams: str | None, limit: int = 2000) -> str:
    combined = " | ".join(stream for stream in streams if stream)
    return (combined.strip() or "no output captured")[-limit:]


def _checkout_repo(repo_url: str, ref: str, dest: Path) -> str:
    """Clone or refresh `dest` to `ref`; return the resolved commit sha.

    Idempotent: a second call against an already-cloned `dest` only fetches
    and re-checks-out the pinned ref, it never re-clones.
    """
    if (dest / ".git").is_dir():
        fetch = _run(["git", "fetch", "origin", ref, "--tags", "--force"], cwd=dest)
        if fetch.returncode != 0:
            raise Mt3ProvisionError(f"git fetch failed for {repo_url}@{ref}: {_tail(fetch.stderr, fetch.stdout)}")
        checkout = _run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = _run(["git", "clone", "--branch", ref, "--no-checkout", repo_url, str(dest)], cwd=dest.parent)
        if clone.returncode != 0:
            raise Mt3ProvisionError(f"git clone failed for {repo_url}@{ref}: {_tail(clone.stderr, clone.stdout)}")
        checkout = _run(["git", "checkout", ref], cwd=dest)
    if checkout.returncode != 0:
        raise Mt3ProvisionError(f"git checkout failed for {repo_url}@{ref}: {_tail(checkout.stderr, checkout.stdout)}")
    resolved = _run(["git", "rev-parse", "HEAD"], cwd=dest)
    if resolved.returncode != 0:
        raise Mt3ProvisionError(f"git rev-parse failed for {repo_url}@{ref}: {_tail(resolved.stderr, resolved.stdout)}")
    return resolved.stdout.strip()


def _build_environment(repo_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run([resolve_uv_executable(), "sync", "--project", str(repo_dir), "--frozen"], cwd=repo_dir)


def _download_model(repo_dir: Path, model_dir: Path) -> subprocess.CompletedProcess[str]:
    model_dir.mkdir(parents=True, exist_ok=True)
    # Do not call the fork's `mt3-download-model`: at the pinned source commit
    # it is deliberately still hard-coded to checkpoint_1002000.  Use its
    # locked huggingface_hub dependency to fetch the selected 4 s checkpoint
    # and normalize its directory name to the stable checkpoint_0 contract.
    downloader = (
        "from huggingface_hub import snapshot_download\n"
        "from pathlib import Path\n"
        "import shutil, sys, tempfile\n"
        "output = Path(sys.argv[1])\n"
        "with tempfile.TemporaryDirectory(dir=output) as temporary:\n"
        "    root = Path(snapshot_download(repo_id=sys.argv[2], revision=sys.argv[3], "
        "allow_patterns=f'{sys.argv[4]}/*', local_dir=temporary))\n"
        "    downloaded = root / sys.argv[4]\n"
        "    if not downloaded.is_dir(): raise RuntimeError(f'missing checkpoint directory: {sys.argv[4]}')\n"
        "    target = output / 'checkpoint_0'\n"
        "    if target.exists(): shutil.rmtree(target)\n"
        "    shutil.move(str(downloaded), str(target))\n"
    )
    return _run(
        [resolve_uv_executable(), "run", "--project", str(repo_dir), "python", "-c", downloader,
         str(model_dir), MT3_HF_REPO_ID, MT3_HF_REVISION, MT3_HF_CHECKPOINT_DIR],
        cwd=repo_dir,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_model_files(model_dir: Path) -> tuple[ModelFileRecord, ...]:
    if not model_dir.is_dir():
        return ()
    records = [
        ModelFileRecord(relative_path=path.relative_to(model_dir).as_posix(), size_bytes=path.stat().st_size, sha256=_sha256_file(path))
        for path in sorted(candidate for candidate in model_dir.rglob("*") if candidate.is_file())
    ]
    return tuple(records)


def _fingerprint(commit: str, files: tuple[ModelFileRecord, ...]) -> str:
    payload = {"commit": commit, "files": [record.to_dict() for record in files]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _model_matches_manifest(model_dir: Path, manifest: CheckpointManifest) -> bool:
    return _hash_model_files(model_dir) == manifest.files


def _load_manifest(path: Path) -> CheckpointManifest | None:
    if not path.is_file():
        return None
    try:
        return CheckpointManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "checkpoint-manifest.json"


def mt3_status(cache_dir: Path | None = None) -> CheckpointManifest | None:
    """Read-only, local-only check of provisioning state; never touches the network.

    Also compares the on-disk manifest's pinned identity (code commit, HF
    checkpoint revision/dir) against this module's current constants. Without
    that check, re-pinning `MT3_PINNED_COMMIT`/`MT3_HF_REVISION`/etc. in code
    would leave a stale already-provisioned checkpoint reporting itself as
    ready, and real transcription runs (`require_mt3_provisioned`) would keep
    silently using the old weights until someone happened to notice and
    manually rerun `vgt transcription backend provision mt3`.
    """
    cache_dir = cache_dir or default_cache_dir()
    manifest = _load_manifest(_manifest_path(cache_dir))
    if manifest is None or not _model_matches_manifest(cache_dir / "models", manifest):
        return None
    if (
        manifest.commit != MT3_PINNED_COMMIT or manifest.model_id != MT3_MODEL_ID
        or manifest.hf_revision != MT3_HF_REVISION or manifest.hf_checkpoint_dir != MT3_HF_CHECKPOINT_DIR
    ):
        return None
    return manifest


def require_mt3_provisioned(cache_dir: Path | None = None) -> CheckpointManifest:
    """Instructions, never network work, when the backend is missing (issue #285)."""
    manifest = mt3_status(cache_dir)
    if manifest is None:
        raise Mt3ProvisionError(
            "the mt3 backend is not provisioned; run `vgt transcription backend provision mt3` first "
            "-- vgt never provisions it implicitly"
        )
    return manifest


def provision_mt3(
    *, cache_dir: Path | None = None, force: bool = False, progress: Callable[[str], None] | None = None,
) -> Mt3ProvisionResult:
    """Provision the pinned mt3 runtime and checkpoint. Idempotent; safe to rerun."""
    cache_dir = cache_dir or default_cache_dir()
    repo_dir, model_dir = cache_dir / "repo", cache_dir / "models"
    emit = progress or (lambda _message: None)

    problems = diagnose_runtime(probe_runtime())
    if problems:
        raise Mt3ProvisionError("cannot provision the mt3 backend on this machine:\n" + "\n".join(f"- {problem}" for problem in problems))

    cache_dir.mkdir(parents=True, exist_ok=True)
    emit(f"checking out mt3 {MT3_PINNED_TAG}")
    commit = _checkout_repo(MT3_REPO_URL, MT3_PINNED_TAG, repo_dir)
    if commit != MT3_PINNED_COMMIT:
        raise Mt3ProvisionError(f"checked-out mt3 commit {commit} does not match the pinned commit {MT3_PINNED_COMMIT} for {MT3_PINNED_TAG}")

    if not force:
        # `mt3_status` is the same identity check (code commit + HF checkpoint
        # revision/dir + on-disk file hashes) that gates real transcription
        # runs, reused here so a re-pin -- even one that only touches the HF
        # checkpoint constants, not MT3_PINNED_COMMIT -- can never be mistaken
        # for "already provisioned" and skip the download.
        existing = mt3_status(cache_dir)
        if existing is not None:
            emit("mt3 backend already provisioned; nothing to do")
            return Mt3ProvisionResult(cache_dir=cache_dir, repo_dir=repo_dir, model_dir=model_dir, manifest=existing, already_provisioned=True)

    emit("building the pinned mt3 environment (uv sync --frozen)")
    build_result = _build_environment(repo_dir)
    if build_result.returncode != 0:
        raise Mt3ProvisionError(f"uv sync --frozen failed for the mt3 environment: {_tail(build_result.stderr, build_result.stdout)}")

    emit("downloading the mt3 checkpoint (mt3-download-model)")
    download_result = _download_model(repo_dir, model_dir)
    if download_result.returncode != 0:
        raise Mt3ProvisionError(f"mt3-download-model failed: {_tail(download_result.stderr, download_result.stdout)}")

    files = _hash_model_files(model_dir)
    if not files:
        raise Mt3ProvisionError("mt3-download-model produced no checkpoint files; rerun provisioning to resume the download")
    empty = [record.relative_path for record in files if record.size_bytes == 0]
    if empty:
        raise Mt3ProvisionError(f"mt3 checkpoint file(s) are empty (corrupt or truncated download): {', '.join(empty)}")

    manifest = CheckpointManifest(
        commit=commit, tag=MT3_PINNED_TAG, files=files, fingerprint=_fingerprint(commit, files),
        model_id=MT3_MODEL_ID, hf_revision=MT3_HF_REVISION, hf_checkpoint_dir=MT3_HF_CHECKPOINT_DIR,
    )
    _manifest_path(cache_dir).write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    emit(f"mt3 backend provisioned (fingerprint {manifest.fingerprint[:12]})")
    return Mt3ProvisionResult(cache_dir=cache_dir, repo_dir=repo_dir, model_dir=model_dir, manifest=manifest, already_provisioned=False)
