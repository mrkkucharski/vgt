#!/usr/bin/env python3
"""Turn an external IDMT-SMT-Drums V2 checkout into benchmark input JSON."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


IDMT_LABELS = {"KD": "kick", "SD": "snare", "HH": "hi_hat_closed"}


def parse_annotation(path: Path) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for event in ET.parse(path).getroot().findall(".//event"):
        label = (event.findtext("instrument") or "").strip()
        onset = event.findtext("onsetSec")
        if label not in IDMT_LABELS or onset is None:
            raise ValueError(f"{path}: unsupported/missing IDMT event fields")
        result.setdefault(IDMT_LABELS[label], []).append(float(onset))
    return result


def build_manifest(dataset_root: Path) -> dict[str, object]:
    annotations = dataset_root / "annotation_xml"
    audio = dataset_root / "audio"
    if not annotations.is_dir() or not audio.is_dir():
        raise ValueError("dataset root must contain annotation_xml/ and audio/")
    clips = []
    for annotation in sorted(annotations.glob("*#MIX.xml")):
        clip_id = annotation.stem
        if not (audio / f"{clip_id}.wav").is_file():
            raise ValueError(f"{annotation}: matching MIX WAV is missing")
        clips.append({"id": clip_id, "audio": f"audio/{clip_id}.wav", "annotations": parse_annotation(annotation)})
    if not clips:
        raise ValueError("no IDMT MIX XML annotations found")
    return {
        "corpus": "IDMT-SMT-Drums V2",
        "source": "Zenodo record 7544164 / IDMT-SMT-DRUMS-V2.zip",
        "annotation_format": "official annotation_xml onsetSec; KD/SD/HH mapped to kick/snare/hi_hat_closed",
        "clip_count": len(clips),
        "clips": clips,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an IDMT-SMT-Drums V2 benchmark manifest; never downloads data.")
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    rendered = json.dumps(build_manifest(args.dataset_root), indent=2, sort_keys=True) + "\n"
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} sha256={hashlib.sha256(rendered.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
