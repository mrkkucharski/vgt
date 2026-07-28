# ADTOF backend Phase 0: feasibility findings

Status: **complete.** This note records the fixed engine contract for Phases
1--3. It does not add runtime code or change the DrumScript default.

## Chosen engine and reproducibility pins

Use the VCS distribution, not PyPI (there is no PyPI release):

```text
adtof-pytorch @ git+https://github.com/xavriley/ADTOF-pytorch.git@85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9
package metadata version: 0.1.0
commit date: 2025-11-11T15:46:47Z
```

This commit is intentional: its `transcribe_to_midi(...,
return_activations=True)` path returns the model tensor before its built-in
peak picker. The installed distribution bundles the converted Frame_RNN
checkpoint; no download or cache lookup is involved at inference:

```text
package resource: adtof_pytorch/data/adtof_frame_rnn_pytorch_weights.pth
size: 3,617,805 bytes
SHA-256: 1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320
```

Pin the source commit and checkpoint hash together in the future `AdtofSpec`.
The upstream package declares unpinned `torch`, `librosa`, `pretty_midi`, and
`numpy`; the spike was run with Python 3.11.15 and `torch==2.13.0` on arm64
macOS. Phase 2 should lock its isolated runner's complete dependency set rather
than inherit these loose upstream requirements.

## Activation contract

The public API was exercised with `device="cpu"`, `model.eval()`, and
`torch.no_grad()` (all set by the port). It returns a `float32` sigmoid-output
array shaped `[1, n_frames, 5]`; the runner must remove the singleton batch axis
and persist `[n_frames, n_classes]` as the raw activation matrix.

Audio preprocessing is mono 44,100 Hz, `n_fft=2048`, and `fps=100`, so the
hop is exactly 441 samples / 10 ms. The STFT uses `center=True`; metadata must
therefore retain `sample_rate`, `hop_samples`, `fps`, `n_fft`, and the upstream
commit as well as the array itself.

Class axis order and the upstream GM labels (`LABELS_5`) are:

| Index | ADTOF class | Upstream GM label | vgt family |
| --- | --- | ---: | --- |
| 0 | Bass drum (BD) | 35 | kick |
| 1 | Snare drum (SD) | 38 | snare |
| 2 | Tom-tom (TT) | 47 | toms |
| 3 | Hi-hat (HH) | 42 | hi-hat |
| 4 | Cymbal (CY) | 49 | cymbals |

This confirms the needed raw-activation seam. vgt must not use the upstream
peak picker/MIDI writer: it owns peak picking, grid association, velocity, and
GM authoring.

## Checkpoint provenance and compliance

The port says the bundled checkpoint was converted from the officially released
Keras weights of [MZehren/ADTOF](https://github.com/MZehren/ADTOF), but it does
not record an original-checkpoint hash or conversion command. The exact
provenance that can currently be reproduced is therefore the bundled file at
the VCS commit and SHA-256 above; it is **not** a separately versioned model
release.

`xavriley/ADTOF-pytorch` has no `LICENSE`, `COPYING`, or `NOTICE` file, and the
GitHub license endpoint reports no detected license. Its code and bundled
converted checkpoint must consequently be recorded as **NOASSERTION / no
upstream licence grant found**, not silently treated as open source. The cited
original ADTOF repository is CC BY-NC-SA 4.0. That source licence does not by
itself establish a licence for this port or its converted binary. For this
non-commercial hobby project, this is a compliance record rather than a go/no-go,
but a future public release must retain the original notices/attribution and
obtain or document a redistribution grant for the port and checkpoint. If the
original CC BY-NC-SA terms govern the artifact, publication must also meet its
attribution, non-commercial, and share-alike obligations.

## Real 7Rivers capture, determinism, and CPU performance

The committed `media/6a7745be/stems/drums.wav` is the required 7Rivers source:
48 kHz stereo PCM, 178.56 s, SHA-256
`6469ee45b8ee3233062031b3a5e447c593a34e2b7612addaa4267fce77bb31d7`.
The raw capture is committed at
`docs/fixtures/adtof-phase-0/7rivers-drums-activations.npz`; it contains the
single `activations` key, a `[17857, 5]` `float32` matrix, and the matrix bytes
hash to `892f972d4af7aa30214daca4b7380ff72d9213bc9dab652215b0eb514c9dccf2`.
Its adjacent JSON is the machine-readable provenance, runtime, and contract
record.

On arm64 macOS with Python 3.11.15 and `torch==2.13.0`, the capture's first
end-to-end model call (model construction, bundled-weight load, audio load,
and CPU prediction) took 21.245631 s; the immediately repeated call took
**1.264965 s (0.00708x real time)**. A fresh subsequent process measured
1.914574 s then 1.258857 s. The large one-time first-call cost is recorded
rather than hidden; use approximately 1.3 s per warmed 178.56 s stem and
allow up to 22 s for a cold process on this machine until Phase 2 measures its
runner overhead.

The two capture calls were bitwise identical (`max_abs_diff=0.0`), and a
separate process reproduced the same matrix SHA-256. This confirms deterministic
CPU eval inference for this pinned setup.

As a non-subjective audio sanity check, local activation maxima at the upstream
thresholds numbered kick 167, snare 146, tom 7, hi-hat 402, and cymbal 0. For
each non-empty class, the median audio onset-strength in a +/-20 ms window
around those peaks was at or above the 98.4th percentile of all 10 ms frames
(kick 99.4, snare 99.2, tom 99.6, hi-hat 98.4). Thus the raw peaks are aligned
with strongly percussive waveform transients, not silence. The zero cymbal
peaks at the port's own 0.30 threshold is retained as a useful Phase 3 tuning
finding, not papered over. Subjective "audible hit" listening remains
user-owned under `docs/AGENTS.md`; this check supplies reproducible objective
evidence without presenting an agent's hearing as human evaluation.

The package includes the checkpoint and its exercised inference path only reads
the local audio and package resource; neither source inspection nor the run
found a network fetch. This supports the Phase 2 requirement that a pre-fetched
runner can infer offline. It does not substitute for an offline-network-sandbox
test in Phase 2.

Reproduce the artifact (the helper is a spike utility, not runtime code) with:

```sh
uv run --isolated --python 3.11 \
  --with 'git+https://github.com/xavriley/ADTOF-pytorch.git@85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9' \
  --with 'torch==2.13.0' \
  python scripts/adtof_phase0_capture.py \
  media/6a7745be/stems/drums.wav \
  docs/fixtures/adtof-phase-0/7rivers-drums-activations
```

It saves the required matrix and JSON with the source/package/checkpoint hashes,
audio duration, CPU/runtime, frame/class contract, and determinism measurement.

## Sources

- [ADTOF-pytorch pinned source](https://github.com/xavriley/ADTOF-pytorch/tree/85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9)
- [ADTOF original source and its CC BY-NC-SA 4.0 notice](https://github.com/MZehren/ADTOF)
