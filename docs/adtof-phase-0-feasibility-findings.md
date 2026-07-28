# ADTOF backend Phase 0: feasibility findings

Status: **partially complete; the real 7Rivers activation capture is blocked by
the absent, user-owned stem.** This note records the fixed engine contract that
Phases 1--3 can use once that capture is made. It does not add runtime code or
change the DrumScript default.

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

## CPU check (upstream bundled test audio)

The required 7Rivers stem is not in this checkout or the accessible local
project workspace, so this is an API/determinism smoke check only, **not the
required real-stem measurement**. On upstream `dev/test.wav` (36.9197 s;
SHA-256 `64612115e2d26c5453024e558ce1144771430a6720a07b64cbf64220a470b1ae`),
three separate warm-cache CPU processes measured 0.855020 s, 0.848526 s, and
0.855836 s end-to-end (median **0.855020 s**, 0.0232x real time). The first
activation capture had shape `[3692, 5]` and byte SHA-256
`285d89efd559f8637ec355c432dd263636226b25435e33178d0fd238a890b32e`.
Two same-process CPU eval runs were bitwise identical (`max_abs_diff=0.0`).

The package includes the checkpoint and its exercised inference path only reads
the local audio and package resource; neither source inspection nor the run
found a network fetch. This supports the Phase 2 requirement that a pre-fetched
runner can infer offline. It does not substitute for an offline-network-sandbox
test in Phase 2.

## Remaining required capture (external input absent)

The issue's specified source, `vgt/6a7745be/stems/drums.wav`, is absent. Do not
replace it with the upstream test clip or commit that clip's activation matrix
as if it were 7Rivers. Once the user-owned stem is made available, run the
pinned isolated environment twice and commit the resulting `npz` plus adjacent
JSON metadata containing at least:

```text
source SHA-256, source duration, package VCS URL + commit, package version,
checkpoint package-resource path + SHA-256, Python/Torch versions, CPU/device,
sample rate, n_fft, hop samples, fps, class names/order/GM labels, matrix shape,
matrix dtype, matrix SHA-256, runtime, and second-run max absolute difference.
```

Perform the requested audible-hit/peak sanity check against that exact source
at the same time. Only then can the Phase 0 real-dump and typical-stem-perf
requirements be marked complete.

## Sources

- [ADTOF-pytorch pinned source](https://github.com/xavriley/ADTOF-pytorch/tree/85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9)
- [ADTOF original source and its CC BY-NC-SA 4.0 notice](https://github.com/MZehren/ADTOF)
