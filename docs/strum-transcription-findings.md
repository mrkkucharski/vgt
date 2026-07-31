# Strum transcription findings

> Phase 1 of [strum-detection-integration-assessment.md](strum-detection-integration-assessment.md).
> This is an evaluation-only result: it does not add a transcription profile,
> a MIDI variant, or any REAPER integration.

Status: **classical detectors rejected on 2026-07-31.** The best measured
candidate has F1 **0.240**, far below the pre-existing Phase 2 bar of roughly
0.75–0.80 F1 for a classical detector on a clean guitar signal. More
importantly, its precision is 17.1%: it emits 58 false strokes in a 23.55 s
window. It must not become a learner-facing reference.

## Reference

`tests/fixtures/strum_7rivers/hand_annotated_onsets.json` contains 30
human-aligned hand-stroke onsets over the continuous audio-relative window
0.193–23.746 s of the committed 7Rivers LALAL `guitar.wav` stem. Every mark is
the leading edge of one strumming-hand stroke, not a per-string note onset.
The annotation is deliberately unquantized. The ±50 ms one-to-one matcher is
the established `drum_evaluation` convention, so an extra peak is a false
positive rather than free recall.

Direction was not annotated. Therefore no audio direction classifier or
grid-alternation direction baseline is reported: either would create labels
the reference does not contain. Onset detection is the only claim measured
here.

## Classical detector measurement

All candidates use librosa 0.11.0, mono audio at the file's native 48 kHz,
512-sample hops, the same reference-blind peak threshold (`delta=0.07`), and
an 80 ms minimum inter-onset interval. `spectral-flux` is standard spectral
flux; `superflux` uses lag 2/local max size 3; `complex-domain` measures the
deviation from the preceding STFT phase trajectory; and
`hpss-percussive-flux` applies spectral flux to the HPSS percussive component.

| detector | predicted | TP | FP | FN | precision | recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| spectral flux | 69 | 10 | 59 | 20 | 14.5% | 33.3% | 20.2% |
| superflux | 63 | 8 | 55 | 22 | 12.7% | 26.7% | 17.2% |
| complex-domain | 45 | 4 | 41 | 26 | 8.9% | 13.3% | 10.7% |
| HPSS percussive + flux | 70 | 12 | 58 | 18 | **17.1%** | **40.0%** | **24.0%** |

The failure is not a near miss. The strongest candidate finds only 12 of 30
strokes and places almost six non-matching peaks for every true one. The dense
false peaks line up with individual string attacks and other guitar/stem
transients; a minimum-onset interval alone cannot identify the hand stroke.

## Decision

Do **not** start Phase 2. Adding the detector as a `guitar-strum` variant would
present a mostly invented rhythm to the learner. Retuning the fixed peak
parameters against this 30-stroke reference would also not establish a robust
detector; it would merely overfit the one excerpt. Keep the script and fixture
as the reproducible negative baseline.

Phase 3 (quantization or direction) depends on a detector worth quantizing and
is therefore not applicable. Phase 4 remains conditional on publicly released,
pinned model weights that can first be measured with this exact harness. The
current papers did not provide those weights when the integration assessment
was written.

## Reproducing

Run locally; this reads the committed stem and writes no vgt state:

```sh
uv run python scripts/strum_detection_probe.py \
  test/7Rivers/vgt/6a7745be/stems/guitar.wav \
  --output /tmp/strum-phase1.json
```

The console table is the concise report and `/tmp/strum-phase1.json` preserves
the full onset lists, settings, counts, and metrics. Normal unit tests use only
the numerical fixture and synthetic arrays, so they do not load the song audio
or download any model or dataset.
