# Guitar transcription findings

> Part of the per-instrument transcription evidence indexed in
> [instrument-transcription-findings.md](instrument-transcription-findings.md),
> which also carries the shared measurement method and the lessons that
> generalize across instruments.

Status: **implemented 2026-07-22 through 2026-07-24** for `guitar_type:
acoustic`, in three rounds. `src/vgt/transcribe.py` now applies the acoustic
threshold overrides plus a five-stage cleanup pipeline (merge → deblip →
clamp → ghost → cap) whenever `guitar_type == "acoustic"`; `electric` and
unset are deliberately left at the original shared defaults, since only the
acoustic case was measured. Each round changes `guitar`'s `settings_hash` for
acoustic-declared projects, so existing acoustic transcriptions correctly
invalidate and re-transcribe on the next run — no migration needed. No round
changes any other target's hash or output.

Read in order: "The complaint" through "Changes applied" is round one (the
threshold retune and the first three cleanup stages); "Round two" covers
fragmentation and isolated blips, and also **corrects a measurement bias in
the round-one sweep table** — see "The chord metric was misleading". "Round
three" adds a librosa-based spectral confirmation gate to the ghost drop, so
that decision is backed by the actual spectrum rather than heuristics alone —
see its "Known limitation" for what remains unmeasured against a real stem.

Investigated 2026-07-22 after a user report that a real project's guitar MIDI
was unusable, then extended after the user inspected the result in REAPER's
piano roll.

## The complaint, quantified

The subject is `7Rivers`, a 178 s track at 120 BPM, transcribed with the
shipping defaults (`onset 0.5`, `frame 0.3`, `min-note 60 ms`, melodia on,
70–1400 Hz) from the LALAL `guitar` stem.  Its recorded `note_count` is 2907.

The instrument is a **steel-string acoustic**, strummed more or less
continuously, and the project correctly declares `guitar_type: acoustic`, so
LALAL's acoustic model produced the stem.  This matters for reading everything
below: the failure is not a distorted-guitar edge case.  Basic Pitch's training
corpus includes GuitarSet, which is acoustic guitar, so this is close to
material the model should handle well — which makes the result below worse than
it first appears, not better.

| Symptom | Measurement |
| --- | --- |
| Sustain runaway | 163 notes longer than 5 s; the longest, a C#2, runs **126 s** |
| Impossible polyphony | **22 simultaneous voices** for 65 % of the song, peaking at 26 |
| Harmonic ghosts | **65 %** of sounding notes sit an octave/12th/double-octave above another note sounding at the same instant |
| Wrong notes | Only **52.5 %** of note-time lands on a tone of the chord vgt itself detected underneath |

The last row is the one that matters.  The output is not merely verbose — at
52.5 % chord agreement it is close to uninformative, so no amount of muting or
filtering in REAPER recovers a usable practice reference from it.

## Root cause

Two shipping defaults interact badly with a continuously strummed acoustic:

- **`DEFAULT_FRAME_THRESHOLD = 0.3`** — a strummed steel-string rings out for
  seconds, and successive chords overlap that ring-out, so the frame
  activations for a pitch rarely fall below 0.3 even after the player has moved
  on.  Notes are therefore never released.  This is what produces the drones
  and, with them, the 22-voice floor.
- **`DEFAULT_MELODIA_TRICK = True`** — melodia then bridges the surviving gaps,
  gluing separate detections into single multi-minute notes.

`DEFAULT_MINIMUM_NOTE_LENGTH_MS = 60` is a secondary contributor: it admits 373
sub-100 ms detections that sit underneath the drones.

The harmonic ghosts have a matching acoustic explanation: a steel-string's
octave and twelfth partials are strong enough, especially in a six-string
strum, to be detected as notes in their own right.  They survive at 30 % even
in the best retuned variant, which is why the cleanup stage below is not
optional.

The frequency bounds are **barely** implicated — see the next section.  The
ghosts are partials of notes *inside* the band, not content above it.

Two further hypotheses were tested and ruled out:

- **Stem quality.** The `guitar` stem is above −40 dBFS-peak for 99 % of its
  length, and only 1.1 % of baseline note-time falls over the near-silent
  remainder.  A silence gate would gain essentially nothing here, and the mess
  is not LALAL bleed.
- **Post-processing alone.** Applying the cleanup described below to the
  *baseline* output still leaves 2077 notes at a 255 ms median.  Polyphony
  becomes legal but the notes remain wrong, because the underlying detections
  are noise.  The settings must be fixed first; cleanup cannot substitute.

## The frequency bounds ignore `guitar_type` (small, but wrong)

`_TARGET_FREQUENCY_HZ["guitar"] = (70.0, 1400.0)` is keyed by target only, and
its comment justifies the floor as *"below drop/Eb-tuned E2"* — reasoning about
an electric that does not apply to a standard-tuned acoustic, whose lowest note
is E2 at 82.4 Hz.  Meanwhile `vgt` already **requires** the caller to declare
`--guitar electric|acoustic`, stores it as `analysis.stems.guitar_type`, and
uses it to pick the LALAL model — so the information needed to narrow the band
correctly is on hand and simply unused.

The baseline output contains 31 notes below E2, worth 2.5 % of note-time,
including part of the 126 s C#2 drone.  These are provably not guitar notes.

But narrowing the band is worth very little on its own:

| variant | bounds | %chordtone |
| --- | --- | ---: |
| baseline | 70–1400 | 52.5 |
| ac_baseline | 80–1200 | 54.1 |
| f65_o60 | 70–1400 | 67.4 |
| ac_f65_o60 | 80–1400 | 67.6 |
| ac_bounds | 80–1200 | 68.0 |

**+1.6 points at baseline settings, +0.6 on top of a retune.**  Worth doing as
a correctness cleanup — a transcription should not contain notes the declared
instrument cannot play — but it is not a fix and should not be sold as one.
Tightening the ceiling to 1000 Hz likewise changed nothing measurable
(`tight` vs `tight_lowmax` below).

## Parameter sweep

Run against the same stem, `basic-pitch[onnx]==0.4.0`, ONNX serialization,
`--midi-tempo 120.004`, 70–1400 Hz unless noted.  `%ghost` is the harmonic-ghost
share, `%>6vc` the share of sounding time above six voices.

> **Caveat added in round two:** `%chordtone` here is the *onset-attributed*
> metric (`%ct-on` in the probe script), which is biased against long notes.
> These variants have broadly similar note lengths so the ranking holds, but
> do not carry these numbers across to a comparison involving merged output —
> use `%ct-t`. See "The chord metric was misleading" below.

| variant | notes | med_ms | max_s | >5s | maxpoly | medpoly | %>6vc | %ghost | %chordtone |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline (shipping) | 2907 | 383 | 126.3 | 163 | 26 | 22 | 100 | 65.4 | 52.5 |
| ac_baseline | 2898 | 372 | 101.6 | 145 | 23 | 20 | 100 | 64.0 | 54.1 |
| nomelodia | 2697 | 337 | 101.6 | 135 | 21 | 19 | 100 | 62.5 | 57.4 |
| frame60 | 3942 | 384 | 21.6 | 100 | 25 | 21 | 99 | 60.3 | 37.5 |
| frame60_nomel | 2596 | 267 | 14.0 | 21 | 20 | 8 | 71 | 43.0 | 54.3 |
| tight | 1113 | 534 | 21.1 | 23 | 17 | 5 | 34 | 36.3 | 63.0 |
| tight_lowmax | 1125 | 534 | 10.7 | 21 | 16 | 5 | 34 | 36.5 | 62.6 |
| **f65_o60** | 1060 | 489 | 6.7 | 6 | 14 | 4 | 15 | 30.2 | **67.4** |
| ac_f65_o60 | 1058 | 489 | 6.7 | 6 | 14 | 4 | 14 | 30.2 | 67.6 |
| **ac_bounds** | 1060 | 489 | 6.7 | 6 | 14 | 4 | 14 | 30.2 | **68.0** |
| **f70_o65** | 627 | 499 | 4.5 | 0 | 14 | 2 | 2 | 22.9 | **75.6** |
| tighter | 433 | 557 | 4.5 | 0 | 12 | 1 | 1 | 17.7 | 78.1 |

Variant settings (all else at the shipping defaults):

| variant | min-note ms | onset | frame | melodia | max Hz |
| --- | ---: | ---: | ---: | --- | ---: |
| baseline | 60 | 0.50 | 0.30 | on | 1400 |
| ac_baseline | 60 | 0.50 | 0.30 | on | 1200 (min 80) |
| nomelodia | 60 | 0.50 | 0.30 | off | 1400 |
| frame60 | 60 | 0.50 | 0.60 | on | 1400 |
| frame60_nomel | 60 | 0.50 | 0.60 | off | 1400 |
| tight | 125 | 0.60 | 0.60 | off | 1400 |
| tight_lowmax | 125 | 0.60 | 0.60 | off | 1000 |
| f65_o60 | 100 | 0.60 | 0.65 | off | 1400 |
| ac_f65_o60 | 100 | 0.60 | 0.65 | off | 1400 (min 80) |
| ac_bounds | 100 | 0.60 | 0.65 | off | 1200 (min 80) |
| f70_o65 | 125 | 0.65 | 0.70 | off | 1400 |
| tighter | 125 | 0.70 | 0.70 | off | 1400 |

Three things to read out of this table:

1. **Chord agreement rises monotonically with tightening**, from 52.5 % to
   78.1 %.  Retuning is not just deleting notes, it is deleting the *wrong*
   notes — otherwise agreement would stay flat as the count fell.
2. **`frame60` is worse than baseline** (37.5 %).  Raising the frame threshold
   while leaving melodia on is actively harmful: releases now happen, and
   melodia reconnects them into more, shorter, wronger notes.  The two settings
   must move together, which is why `nomelodia` alone is also insufficient.
3. **Even the best variants exceed six voices** (`f65_o60` peaks at 14).  No
   Basic Pitch setting fixes this; it needs the cleanup stage below.

`tighter` scores highest on chord agreement but at 433 notes over 178 s is
likely dropping real passing notes, and its score is partly an artifact of
keeping only the most confident (therefore most chord-tone-ish) detections.
`f65_o60` and `f70_o65` are the honest candidates.

## Changes applied

All four proposals below are now implemented in `src/vgt/transcribe.py`,
gated on `guitar_type == "acoustic"` throughout:

1. **Retuned guitar defaults, acoustic only.** `GUITAR_ACOUSTIC_ONSET_THRESHOLD
   = 0.6`, `GUITAR_ACOUSTIC_FRAME_THRESHOLD = 0.65`,
   `GUITAR_ACOUSTIC_MINIMUM_NOTE_LENGTH_MS = 100.0`,
   `GUITAR_ACOUSTIC_MELODIA_TRICK = False`. These are separate constants from
   the shared `DEFAULT_*` ones, applied only inside `default_spec_for_target`
   when `target == "guitar" and guitar_type == "acoustic"` — `electric` and
   unset fall through to the original shared defaults untouched, and every
   other target's `settings_hash` is unaffected (verified by
   `test_default_spec_leaves_electric_and_unset_guitar_at_the_generic_defaults`
   and `test_default_spec_acoustic_override_is_guitar_only`).

   This is still measured on one acoustic track only. Whether a distorted
   electric wants the same numbers, or different ones, remains unmeasured —
   `guitar_type: electric` intentionally does not get these thresholds.

2. **`guitar_type` (and `time_signature`) now reach the spec.**
   `default_spec_for_target` takes both; `TargetTranscriberRouter.spec_for_target`
   and the `TranscriberRouter` protocol thread them through;
   `analysis._refresh_target` reads `analysis.stems.guitar_type` and
   `analysis.tempo.value.time_signature` and passes them down. This is what
   lets 80–1200 Hz apply only to a declared acoustic guitar, and lets the
   sustain clamp below convert bars to seconds at the song's actual tempo and
   signature.

3. **Post-transcription cleanup pass**, run inside
   `BasicPitchTranscriber.transcribe` whenever the spec requests it:
   `_clamp_sustain` → `_drop_harmonic_ghosts` → `_cap_simultaneous_voices`
   (that order — the clamp must run first or a still-runaway drone would
   dominate which voices the cap retires; the ghost drop runs on
   already-clamped durations). The voice cap truncates the quietest
   **already-sounding** voice at a new note's onset rather than deleting
   it outright, so the onset still lands on the reference track — it does not
   reject a new note for being the quieter arrival, since by the time a note
   is a candidate for the cut it has already survived the ghost drop.

   The clamp went from a fixed 4 s to `GUITAR_SUSTAIN_CLAMP_BARS = 2.0` bars,
   resolved to seconds via `_bar_duration_seconds(bpm, time_signature)` at
   spec-construction time (defaulting to 4/4 when the signature is missing,
   same fallback `tempo.py` already uses) — the generalisation flagged below
   the original measurement, so slower material isn't clamped tighter than
   this track was.

4. **Six-voice limit enforced unconditionally** whenever cleanup runs, via
   `GUITAR_MAX_SIMULTANEOUS_VOICES = 6` and `_cap_simultaneous_voices` — not a
   configurable knob, so nothing downstream can emit a >6-voice guitar chord
   once acoustic cleanup is active.

Both the retuned thresholds and the cleanup fields live on `BasicPitchSpec`
itself (three new optional fields, `None`/`False` for every non-guitar
target), so a future change to any of them naturally invalidates
`settings_hash` and forces re-transcription — no separate cache-key needed.
Adding the fields did, once, bump every existing basic-pitch target's stored
hash (the dataclass gained columns), which is an expected one-time,
zero-cost re-transcription, not a correctness concern.

Test coverage: `tests/test_transcribe.py` covers the acoustic-vs-electric spec
split, the bar-to-seconds conversion (including the no-tempo-yet case), each
cleanup function in isolation (including the "new note quieter than what it
displaces" edge case the eviction rule does *not* handle, and the "already
truncated voice must stay truncated for later overlap checks" case), the
full pipeline ordering, and one end-to-end `BasicPitchTranscriber.transcribe`
run against a fake subprocess producing a runaway note, verifying both the
rewritten CSV and MIDI reflect the cleaned result.

## Real-world verification: `7Rivers` re-transcribed

After implementing, `7Rivers`'s guitar target was reset
(`vgt analyze --forget-transcription guitar`, then `--transcribe guitar`) and
re-transcribed against the real acoustic stem through the normal CLI path —
not the sweep script. Before/after, measured by
`scripts/guitar_transcription_probe.py` against the actual sidecar output:

| | notes | med_ms | max_s | >5s | maxpoly | %ghost | %chordtone |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| before (this doc's baseline) | 2907 | 383 | 126.3 | 163 | 26 | 65.4 | 52.5 |
| after (production output) | 897 | 488 | **4.0** | **0** | **6** | 19.6 | **70.5** |

Every invariant holds exactly: no note exceeds the 4 s (2-bar, 120 BPM) clamp,
and polyphony never exceeds 6. Chord agreement (70.5 %) came out slightly
*above* the sweep's own `ac_bounds` measurement (69.9 %, prototype cleanup on
the `f65_o60` sweep variant) — consistent with, not merely close to, the
sweep's prediction. `settings_hash` changed as expected
(`b679a47b5e...` → `dd64f73e47...`), so no manual sidecar edit was needed:
`--forget-transcription` deleted the stale `guitar.mid`/`.csv` and the
persisted entry, and the ordinary transcription stage detected the missing
target and re-ran it against the new spec. Only `guitar`'s entry changed;
`drums` reported `unchanged, using cached result` in both runs.

## Round two: fragmentation and isolated blips

The user inspected the 897-note result in REAPER's piano roll and reported two
remaining artifacts: held notes broken by hairline gaps, and very short notes
sitting alone with nothing around them.

### Both confirmed, and the first is the dominant one

Same-pitch gap distribution in the 897-note output:

| gap | count |
| --- | ---: |
| **exactly 0** | **390** |
| 0–10 ms | 0 |
| 10–20 ms | 5 |
| 20–50 ms | 23 |
| 50–100 ms | 12 |
| 100–300 ms | 45 |
| >300 ms | 384 |

390 of 435 sub-300 ms gaps are *exactly zero-width* — a note ending and the
next beginning at the identical timestamp, which is a split, not a
re-articulation. Nothing at all falls between 0 and 10 ms, and there is a
clean cliff before genuine repeated notes above 300 ms. That bimodality is
what makes merging safe here: there is no ambiguous middle band to get wrong.

This artifact is a **direct consequence of the round-one retune**. Raising
`frame_threshold` to 0.65 is what stopped the drones, but it also means a held
note whose activation dips below the threshold mid-way gets emitted as two
notes. The two failure modes trade off against each other and no single
threshold avoids both — which is why the fix belongs in post-processing.

Isolated blips are real but a much smaller effect: ~17 notes matched "under
150 ms with no same-pitch neighbour within ±1 s".

### Changes applied

Two new passes, `_merge_fragments` and `_drop_isolated_notes`, inserted at the
**front** of the cleanup pipeline. The full order is now merge → deblip →
clamp → ghost → cap, and each step depends on the ones before it (see
`_GUITAR_ACOUSTIC_PROFILE`'s docstring in `transcribe.py`, where this ordering
now lives).

`GUITAR_FRAGMENT_MERGE_GAP_S = 0.03` is deliberately *not* tempo-scaled,
unlike the sustain clamp. It describes a model artifact measured in analysis
frames, not a musical duration; a bar-relative gap would grow at slow tempos
and start swallowing genuine repeated notes.

### Ordering is load-bearing — and my first test for it was wrong

Merging must run before the clamp and the voice cap, or it re-extends notes
past decisions those stages already made. Merging the *finished* 897-note
output re-created a 7.1 s note under a 4 s clamp, and at a 30 ms gap pushed
polyphony from 6 to 7 — breaking both invariants.

The first test written for this asserted polyphony stayed ≤ 6 after cleanup.
It **passed even with the merge deliberately moved to last**, because the
voice cap can satisfy the assertion by dropping the offending pitch outright.
The replacement asserts the sustain-clamp property instead — two 3.5 s
fragments are each individually under the 4 s clamp, so clamping first leaves
both alone and a later merge yields a 7 s note. That version was verified to
fail under the wrong order before being kept. **An ordering test that has not
been run against the wrong order is not evidence of anything.**

### The chord metric was misleading, and is now fixed

Merging appeared to *drop* chord agreement from 70.5 % to 63.3 %. It doesn't:
the metric attributes each note wholly to the chord under its onset, so a
merged note ringing across a chord change is penalised for its full length,
while its fragments were each credited to their own chord. Scored
length-neutrally (sampling every 20 ms against the chord sounding at that
instant), merging is quality-neutral.

`guitar_transcription_probe.py` now reports both, as `%ct-on` and `%ct-t`.
**The `%chordtone` column in the parameter-sweep table above is the
onset-attributed one and is biased against long notes** — it is sound for
ranking variants of similar note length (which those were), but must not be
used to compare variants whose note lengths differ.

### Result

| | notes | med_ms | max_s | maxpoly | frag | %ghost | %ct-on | %ct-t |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| original baseline | 2907 | 383 | 126.3 | 26 | 1995 | 65.4 | 52.5 | 43.1 |
| after round one | 897 | 488 | 4.0 | 6 | 405 | 19.6 | 70.5 | 65.1 |
| after round two | 443 | 952 | 4.0 | **6** | **0** | 18.6 | 63.3 | **65.3** |

Fragmentation is fully eliminated, the clamp and six-voice invariants still
hold, and length-neutral chord agreement is unchanged (65.1 → 65.3). On the
honest metric the whole exercise moved the reference from 43.1 % to 65.3 %.

The sustain clamp was re-examined rather than assumed: after merging it
engages on 5.2 % of notes and removes 6.9 % of total sounding time (up from
1.0 % / 2.0 %). That increase is the clamp doing its job — a merged chain of
fragments is exactly how a drone survives the merge step — so it was left at
two bars.

### Test coverage added

`_merge_fragments`: zero-width split rejoined, three-fragment chain collapsed
in one pass, genuine re-articulation left alone, different pitches never
joined, loudest fragment's velocity kept, and an *overlapping* same-pitch pair
merged to the later end rather than truncated. `_drop_isolated_notes`: lone
blip removed, short note inside a same-pitch run kept, long isolated note kept
(isolation alone is not suspicious — only isolation plus brevity). Plus the
ordering test described above, verified to fail under the wrong order.

### Known limitation

Two isolated short notes survive in the final output. Both are explained, and
neither is a defect: one is a 511 ms note the *voice cap* truncated to 104 ms
after the deblip pass had already run, and the other lost its only neighbour
to a later stage. Isolated-blip removal is therefore **best-effort, not an
invariant** like the clamp, fragmentation, and voice count. Moving the deblip
pass after the voice cap would catch both, but at the cost of deleting the
cap's deliberately-retained onset stubs — the cap truncates rather than
deletes precisely so a retired note's onset still appears on the reference
track.

## Round three: spectral confirmation for the ghost drop (#144)

`_drop_harmonic_ghosts` (round one) decides "ghost vs. real note" purely from
note-level heuristics — the harmonic interval, near-simultaneous onset,
overlap fraction, and velocity slack. It never looked at the audio, so it
fundamentally could not distinguish an intentionally played octave/12th from a
ringing partial; the docstring conceded a real note at a harmonic interval
only "usually" survives.

### The gate

`_ghost_has_independent_energy` (`src/vgt/transcribe.py`) implements the
*collapsing harmonics* technique (harmonic masking / matching pursuit): for a
note the heuristic has already flagged, it fits a log-linear decay curve to
the lower parent note's *other* visible harmonics (excluding the one the
ghost's pitch coincides with) over their shared overlap window, then compares
the measured amplitude at the ghost's own fundamental against that curve's
prediction. Amplitude well above the prediction (by
`GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO`, `1.5`×) means something
independent is sounding there, so the note survives; amplitude at or below the
prediction means the parent's own harmonic series already explains it, so the
heuristic's drop is confirmed.

This is a **gate, not a new detector**: a flagged note is dropped only when
the heuristic *and* the spectral check agree. Whenever the audio can't settle
the question (no true overlap window, or fewer than two of the parent's other
harmonics are visible to fit a curve), the gate returns "keep" conservatively
— absence of evidence never adds a drop, it only ever withholds one the
heuristic already decided. This means the gate can only ever *retain* notes
the old heuristic-only pipeline would have dropped; it cannot cause a note the
heuristic already kept to be dropped, and it cannot catch a ghost the interval
templates miss (out of scope, per the issue).

### Plumbing

The stem audio is the same `source` path `BasicPitchTranscriber.transcribe`
already passes to Basic Pitch — no new path is threaded through. `_apply_cleanup_stages`
loads it and computes one STFT, lazily and at most once, the first time it
reaches a `drop_harmonic_ghosts` stage; a target whose cleanup pipeline never
includes that stage (every target except `guitar-acoustic`) never imports
librosa or touches the audio. The three new thresholds
(`spectral_max_harmonic_order`, `spectral_freq_tolerance_semitones`,
`spectral_independent_energy_ratio`) live in that stage's `CleanupStage.params`
alongside the existing four, so they flow into `settings_hash` the same way —
adding them moved `guitar-acoustic`'s hash once, an expected one-time
invalidation of only that target's cached transcription (verified in
`tests/test_analysis.py`'s v9-migration hash test, updated alongside this
change).

### Test coverage

`tests/test_transcribe.py` synthesizes short WAV fixtures with `soundfile`
(already a hard dependency) — a fundamental plus a clean, several-harmonic
geometric decay series — and covers three cases directly against
`_drop_harmonic_ghosts`: a real octave whose fundamental carries strong energy
far above what the decay curve predicts (kept), a pure partial whose energy
sits exactly on the decay curve (dropped, i.e. the pre-existing heuristic
behaviour is preserved), and a non-concurrent pair the heuristic itself never
flags, confirming the gate cannot widen a drop regardless of what the
spectrum shows. A fourth test confirms a pipeline with no ghost-drop stage
never calls the audio loader. All four run offline, on numpy-generated tones
— no model, network, or real stem required.

### Known limitation: not yet re-measured against the real `7Rivers` stem

The "Real-world verification" section above (round one) and the round-two
results were measured against an actual LALAL-separated acoustic guitar stem
that lives in the reporting user's own project, not in this repository —
consistent with vgt's own rule that it never commits stem audio or other
per-project artifacts into the codebase. This implementation environment has
no access to that file (or any equivalent real acoustic guitar recording), so
the before/after ghost-drop counts and "real octaves preserved" measurement
the issue asks for could not be produced here.

What *is* verified here: the gate's logic against synthetic audio (above),
that it changes nothing for every non-`guitar-acoustic` target, and that it
can only narrow — never widen — round one's ghost-drop behaviour. Re-running
`vgt analyze --forget-transcription guitar --transcribe guitar` against the
real `7Rivers` project (same command as round one's verification) and
diffing `guitar_transcription_probe.py`'s `%ghost` column before/after would
close this out; the `settings_hash` change already guarantees that run will
re-transcribe rather than reuse the stale cache.

## Ruled out: converting the stem to mono

The LALAL guitar stem is a 48 kHz stereo file, which raises the reasonable
question of whether pre-converting it to mono would give Basic Pitch a
cleaner signal. It would not, for two independent reasons.

**Basic Pitch already downmixes.** `basic_pitch/inference.py` loads audio with
`librosa.load(path, sr=AUDIO_SAMPLE_RATE, mono=True)` — the model only ever
sees 22050 Hz mono, whatever the input file's channel count or sample rate.
Verified rather than inferred from the source: a mono copy of the stem
transcribed at the production settings produced 1060 notes with a note list
**identical row for row** (onsets, offsets, pitches, velocities) to the stereo
run. Pre-conversion is a no-op.

**There is nothing to cancel anyway.** The one case where channel handling
could matter is an L+R average nulling content — which would call for picking
a channel, not for "converting to mono". This stem's image is far too narrow
for that to bite:

| measure | value |
| --- | ---: |
| L/R correlation | 0.932 |
| mono RMS vs mean single-channel RMS | 0.983 (≈0.15 dB) |
| side/mid ratio | 0.188 |
| worst per-band downmix loss, 80 Hz–5 kHz | −0.48 dB |

The same reasoning covers sample rate: 48 kHz is resampled to 22050 Hz
regardless. **No pre-conditioning of the file's container format — channels,
rate, bit depth — can change what the model sees.** Only changes to the
*content* (EQ, transient shaping, a different separation model) could.

## Reproducing

`scripts/guitar_transcription_probe.py` computes every metric in this document
from a Basic Pitch note-events CSV.  It runs no model and writes nothing into a
vgt project:

```sh
uv run python scripts/guitar_transcription_probe.py \
  /path/to/project/vgt/<namespace>/transcription/guitar.csv \
  --profile guitar-acoustic \
  --chords /path/to/project/vgt/<namespace>/chords.txt
```

Passing several CSVs compares parameter variants side by side; when they share
a filename the containing directory is used as the label, so a sweep laid out
as `sweep/<variant>/guitar_basic_pitch.csv` reads directly.

`guitar-acoustic` is the default profile, retained so older profile-less
commands reproduce these findings. Select a different profile only when that
instrument has measured probe expectations.

Column guide: `frag` counts adjacent same-pitch pairs no more than 30 ms apart
(should be 0 once the merge pass has run); `%ct-on` and `%ct-t` are the
onset-attributed and time-attributed chord agreements respectively. **Prefer
`%ct-t` whenever the variants differ in note length** — `%ct-on` penalises a
long note for ringing across a chord change and will make merged output look
worse than it is.

To regenerate the sweep, install the backend once and drive it through the same
command line `build_basic_pitch_argv` produces:

```sh
uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"
basic-pitch sweep/f65_o60 /path/to/stems/guitar.wav \
  --model-serialization onnx --save-note-events --midi-tempo 120.004 \
  --minimum-frequency 70 --maximum-frequency 1400 \
  --minimum-note-length 100 --onset-threshold 0.6 --frame-threshold 0.65 --no-melodia
```

Setting `VGT_BASIC_PITCH_CMD=basic-pitch` afterwards points vgt at that
installed binary and skips the ~35 s cold `uvx` build per run — the escape
hatch `BASIC_PITCH_CMD_ENV` already documents.

The chord-agreement metric depends on vgt's own `chords.txt`, which is itself
estimated and collapsed to `maj_min`.  It is a useful *relative* signal for
ranking variants and should not be read as an absolute accuracy figure.

## MT3 alternative (opt-in comparison, issue #288)

Status: **shipped as `guitar-mt3`, opt-in, no fallback, single-song evidence
only** (2026-08-03). `guitar-mt3` feeds the raw `guitar` stem (no HPSS
frontend) into the pinned MT3 backend (`docs/instrument-transcription-
findings.md` links the backend's own provisioning/normalization docs) and
retains only its first note-bearing MIDI track, unmodified by any cleanup
stage. It never replaces or changes `guitar`'s existing Basic Pitch defaults,
and there is no fallback if MT3 is unavailable or fails.

Measured against the same `7Rivers` stem and the same `guitar-acoustic`
comparison harness as the rest of this document, `guitar-mt3` beside the
shipped `guitar-acoustic-clean` default:

| Variant | notes | med (ms) | max (s) | >5s | maxpoly | %ghost | %ct-on | %ct-t | frag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `guitar-acoustic-clean` (default) | 481 | 895 | 4.0 | 0 | 6 | 22.8 | 67.9 | 68.4 | 0 |
| `guitar-mt3` | 1804 | 241 | 2.4 | 0 | 8 | 31.7 | 86.2 | 94.4 | **1228** |

Two things stand out, in opposite directions:

- **No sustain runaway, and chord-tone agreement is clearly higher** (94.4%
  time-attributed vs. 68.4%). MT3 does not share Basic Pitch's frame-release
  failure mode on this stem (see finding 2 in the index): nothing rings past
  4.4 s, let alone the 126 s drone the unmodified defaults produced before
  acoustic tuning existed.
- **Fragmentation is severe.** 1228 of 1804 notes are same-pitch pairs within
  30 ms of each other — most of MT3's output is a held note chopped into many
  short repeated fragments rather than one continuous note, which is exactly
  the failure `merge_fragments` exists to repair for Basic Pitch and is not
  applied here by design (see "Profile behavior" above). A high chord-tone
  score does not mean this is directly usable as-is: pitch correctness and
  note-count/duration sanity are different questions, and this profile
  deliberately reports only the former honestly, unfiltered.

This is **one song, one run, no fallback** — read it exactly like the standing
caveat at the top of the index says: a relative ranking signal on `7Rivers`,
not an absolute accuracy claim, and not yet reason to prefer or promote this
profile. It has not been listened to end-to-end in REAPER; that verification
step remains the user's.

### Reproducing

```sh
vgt transcription backend provision mt3
vgt transcription variant add guitar --name mt3 --profile guitar-mt3 "Song.RPP"
uv run python scripts/guitar_transcription_probe.py \
  /path/to/project/vgt/<namespace>/transcription/guitar/<default-id>.csv \
  /path/to/project/vgt/<namespace>/transcription/guitar/<mt3-id>.csv \
  --profile guitar-acoustic \
  --chords /path/to/project/vgt/<namespace>/chords.txt
```
