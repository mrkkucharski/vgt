# Section detection tuning

## Why the fallback over-segmented songs

The librosa fallback originally computed its checkerboard novelty curve at the
native 512-sample feature hop and used a 32-frame half-kernel. The source
comment described that as "a few seconds" of context, but it is only about
0.37 seconds at 44.1 kHz and 1.02 seconds at 16 kHz. Combined with a 4-second
minimum gap and a `0.05` peak delta, the detector responded to local phrases,
fills, and texture fluctuations rather than changes in the character of the
music.

Issue #284 used two human-corrected timelines as references:

- `test/7Rivers`: 9 regions instead of the fallback's 23.
- `Perfect_Chcemy-byc-soba` (owner-local audio, not redistributed): 8 regions
  instead of the fallback's 37.

## Measurement

`scripts/section_detection_probe.py` performs one-to-one boundary matching at
a four-second tolerance. One predicted boundary can match at most one reference
boundary, so dense over-detection is penalized as false positives. This is a
section-level tolerance (roughly two 4/4 bars at 120 BPM), not a claim that the
detector places an edit-ready boundary exactly on a beat.

The selected fallback pools chroma+MFCC descriptors to 2 Hz, measures novelty
with eight seconds of context on each side, finds local candidates in a
four-second window at delta `0.10`, then applies strength-first non-maximum
suppression with a 15-second minimum gap. Candidate generation and pruning are
separate so an early weak phrase change cannot hide a stronger nearby section
change.

| Song | Version | Regions | TP | FP | FN | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 7Rivers | Previous | 23 | 8 | 14 | 0 | 0.364 | 1.000 | 0.533 |
| 7Rivers | Tuned | 8 | 5 | 2 | 3 | 0.714 | 0.625 | 0.667 |
| Perfect | Previous | 37 | 7 | 29 | 0 | 0.194 | 1.000 | 0.326 |
| Perfect | Tuned | 7 | 6 | 0 | 1 | 1.000 | 0.857 | 0.923 |

Macro F1 rises from `0.429` to `0.795`; the total false-positive count falls
from 43 to 2. The tuned region counts are close to the corrected counts without
using a requested region count or song-specific timestamps.

Reproduce the comparison while the sidecars still preserve the previous
`detected` baseline:

```console
uv run python scripts/section_detection_probe.py \
  "test/7Rivers/Media/The Seven Rivers (Full March - 3_00).mp3" \
  test/7Rivers/7Rivers.vgt \
  "/path/to/02.Perfect - Chcemy byc soba.m4a" \
  "/path/to/Chcemy Bys Soba.vgt"
```

## Limits

- This is evidence from two songs, not a broad genre benchmark. In particular,
  three corrected 7Rivers boundaries are missed at the four-second tolerance.
  The change deliberately favors a small set of defensible character changes
  over the old fallback's perfect recall surrounded by many false regions.
- The tuning changes the always-available librosa fallback. An installed and
  working MSAF backend still owns its own segmentation behavior.
- Existing human-verified section values remain untouched. `vgt analyze
  --force` refreshes only their machine-detected baseline; it does not discard
  the corrected effective regions.
