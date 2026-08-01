# Experiment plan: targeted post-separation cleanup for transcription

## Decision

VGT will **not** apply a blanket “maximize stem SNR” chain to every LALAL
stem. The supplied survey (`Maximizinf_stem_SNR.pdf`) is useful as a list of
hypotheses, but it is speech-heavy, does not supply recoverable primary
citations, and includes schematic code rather than a production recipe.
LALAL stems are estimates from a mastered music mix, not independent noisy
microphone recordings. A speech denoiser, WPE dereverberator, or generic
compressor can remove attacks, bass fundamentals, cymbal tails, harmonies, and
intentional reverberation needed by the transcription engines.

Instead, VGT provides opt-in, content-addressed **analysis frontends**. They
derive an aligned WAV for one transcription variant while leaving the raw LALAL
stem and normal REAPER stem track untouched. A frontend earns retention only by
improving musical transcription metrics.

The 7Rivers stems are 48 kHz stereo PCM, all 178.56 seconds long and unclipped.
Their level differences do not demonstrate an SNR defect: uniform loudness
normalization changes level, not signal-to-interference ratio. VGT has no clean
ground-truth stems from which to calculate real SNR.

## Scope and candidates

The first experiment is deliberately limited to deterministic DSP already
available through VGT's `librosa`/SciPy stack. It does not install or run
RNNoise, DeepFilterNet, WPE, a second separator, pitch correction, or generic
compression.

| Target | Baseline | Independent candidates | Metric |
| --- | --- | --- | --- |
| Drums | `drums-clean` with raw stem | 60% percussive HPSS; conservative soft gate | onset P/R/F, false events in breaks, timing |
| Bass | `bass` with raw stem | 30–600 Hz band-pass; 50% harmonic HPSS | onset F, frame F, octave error, folds |
| Acoustic guitar | `guitar-acoustic-clean` with raw stem | 70–5000 Hz band-pass; 50% harmonic HPSS | chord-time agreement, polyphony, fragmentation, ghosts |

Run candidates alone first. Combine stages only when each stage independently
beats its baseline. The exact configuration lives in a project-local
`<project>.vgt-profiles.toml` profile, so all settings are inspectable and
reproducible.

## Data flow and cache contract

```text
raw LALAL stem (immutable)
       │ raw SHA + recipe + frontend version
       ▼
transcription/cache/audio-frontends/<hash>/analysis.wav
       │ processed SHA
       ▼
existing backend and note cleanup → retained MIDI/CSV/JSON variant
```

The frontend preserves sample rate, stereo channels, frame count, and duration.
It writes atomically and rejects non-finite or malformed output. Variant records
retain both the raw stem hash (`source_input_hash`) and the derivative hash
(`analysis_input_hash`), recipe, and relative derivative path. Detection caches
are keyed by the derivative bytes, so raw and cleaned runs never collide.

The raw baseline has an empty frontend and preserves the existing hash and cache
behavior. Frontend audio is analysis-only; it never overwrites `stems/*.wav` or
causes new LALAL work.

## Step-by-step execution

1. Copy the 7Rivers project to a disposable output directory. Never mutate
   `test/7Rivers/`.
2. Add baseline variants and project-local frontend profiles for all candidates.
3. Run `vgt transcription profile validate` before invoking a backend.
4. Add every candidate through `vgt transcription variant add`; VGT creates the
   derived WAV, then transcribes it with unchanged target/backend settings.
5. Score drum output with `scripts/drum_midi_score.py`, bass with
   `scripts/bass_transcription_probe.py --onset-reference`, and guitar with
   `scripts/guitar_transcription_probe.py`.
6. Produce a Markdown/JSON table that includes the baseline and every candidate,
   including duration/alignment checks and exact profile hashes.
7. Reject regressions before trying any combined frontend.
8. Open the disposable project in REAPER and run the normal initialize action.
   It creates a muted `[vgt] <Target> Analysis — <label> (Audio)` track beside
   each processed MIDI variant. The raw stem stays the audible baseline.
9. Human review: solo raw/processed audio, compare their adjacent MIDI, check
   quiet hits, attacks, ring-out, bleed, timing, and audible artifacts. Record
   observations in the experiment result.
10. Keep winning profiles opt-in. Do not change a default until a second song
    reproduces the improvement against an independent reference.

## Promotion bars

- Drums: at least +2 absolute F1, fewer false events in annotated breaks,
  recall loss at most 2 points, and no more than 10 ms timing regression.
- Bass: at least +2 onset-F1, frame-F loss under 1 point, no increased octave
  error, and no regression on either alternating fold.
- Guitar: at least +2 time-weighted chord agreement with no worse physical
  polyphony or fragmentation. Human listening is required because the existing
  automatic reference is indirect.

## What is deliberately deferred

LALAL itself exposes a dereverb switch, but that changes paid separation rather
than post-processing and is not included here. De-bleed regression also remains
out of scope: VGT has estimated musical stems rather than time-aligned microphone
tracks with a known bleed source. Any future neural music-specific restoration
model needs a separate benchmark, pinned model/runtime identity, and the same
raw-vs-derived A/B contract.
