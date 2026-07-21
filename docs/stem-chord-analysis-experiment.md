# Experiment: does chord detection on stems beat chord detection on the mix?

**Question.** Phase 1 detects chords from the reference mix only. Now that Phase 2
produces stems, can analyzing `instrumental`, `bass`, `guitar`, and `backing`
*in addition to* the original track improve chord quality?

**Answer.** Yes — but less than the raw numbers first suggest, and not in the way
you'd expect. Roughly two-thirds of the apparent gain turns out to come from a
better *cleanup step*, which needs no stems at all. The stems earn the rest, and
they earn it mostly on chord **roots** rather than on major/minor.

---

## Part 1 — How this chord detector works

Skip to Part 2 if you already know. Otherwise this is worth four minutes, because
the results only make sense once you can see which step each number is testing.

The detector has four steps. Every pipeline compared below uses the same first two.

### Step 1 — measure which notes are sounding

Chop the audio into short slices. For each slice, measure how much energy sits on
each of the 12 pitch names (C, C#, D, … B), ignoring which octave it's in. A slice
becomes 12 numbers: *"lots of A#, lots of C#, lots of F, little else."*

That 12-number vector is called a **chroma**. It is the only thing the detector
ever sees of your music.

### Step 2 — guess a chord from those 12 numbers

We keep 24 templates, one for every chord we're willing to name — 12 roots × two
flavors:

- A# **major** = the notes A#, D, F
- A# **minor** = the notes A#, C#, F
- …and so on for all 12 roots.

Compare the slice's 12 numbers against all 24 templates and pick the closest match.
Do that once per beat, and 178 seconds of audio becomes 354 chord guesses.

That's the entire detector. Note what's crude about it, because everything below
follows from these two weaknesses:

1. **Each beat is guessed completely independently** — no memory of the previous beat.
2. **It only knows 24 chords.** Sevenths, sus chords, add9 — all collapse to the
   nearest major or minor. (This is deliberate; the sidecar flags it as
   `vocabulary: maj_min`.)

### Step 3 — clean up the noisy guesses

Because of weakness #1, raw output flickers beat to beat:
`A#min, A#min, F#maj, A#min, A#min…` — obviously that F#maj is a glitch, not a
real chord change. Something has to smooth it out.

This step is what I call the **decoder**, and it's where two of the three pipelines
below differ. Two approaches were compared:

- **Majority filter** — *what vgt ships today.* For each beat, look at its two
  neighbors on each side and take whichever label appears most often. It kills
  flicker, but it's blunt: it has no notion of a bar, and no notion that chords
  are *held*.
- **Bar aggregation + duration prior** — two ideas together. First, average the
  evidence across each group of 4 beats **before** deciding, so one bar produces
  one well-supported answer instead of four shaky ones. Second, add a bonus for
  keeping the same chord as the previous beat, so changing chord has to be
  actively justified by the audio. Both encode something true about music that the
  majority filter simply doesn't know: chords last, and they tend to change at bar
  lines.

### Step 4 — score it

Compare against your human-verified chord sheet, beat by beat. Two scores are
reported throughout, and the difference between them matters:

For a chord written `A#:min` — **A#** is the *root* (which note the chord is built
on) and **min** is the *quality* (major or minor — is the third bright or dark).

- **root** = did we get the letter right?
- **exact** = did we get the letter *and* the major/minor right?

Root is the easier, more robust question. Exact is what you actually want printed
on the chord track.

---

## Part 2 — The two knobs, and the three pipelines

Everything in this experiment is a combination of just two independent choices.

### Knob A — what audio do you look at in Step 1?

- **The mix.** One chroma per beat, measured from the full original track. The
  problem: vocals, cymbals and snare hits all dump energy onto pitches that have
  nothing to do with the chord. The harmony is real but buried under noise.
- **The stems.** Measure a chroma from the original **and** from the instrumental,
  guitar, and backing stems — four opinions per beat instead of one. Add up their
  24 template scores and take the winner. The guitar stem has the vocals and drums
  stripped out, so its 12 numbers are far cleaner harmonically. When it and the mix
  agree, that's a strong signal; when the mix is confused by a cymbal crash, the
  guitar can outvote it.

### Knob B — how do you clean up in Step 3?

The majority filter, or bar aggregation + duration prior, as described above.

### The three pipelines

| pipeline | audio (knob A) | cleanup (knob B) | exact | root |
|---|---|---|---|---|
| **shipped today** | mix only | majority filter | 83.6% | 89.0% |
| **decoder-only control** | mix only | bar-agg + duration prior | 88.7% | 92.1% |
| **stem fusion** | mix + 3 stems | bar-agg + duration prior | **90.7%** | **95.2%** |

**The middle row is the whole point of the experiment being trustworthy.** Without
it, the honest-sounding claim would be "stems take you from 83.6% to 90.7%" — and
you'd conclude the stems earned all 7 points. They didn't. Changing *only* the
cleanup step, on the same mix audio, with no stems whatsoever, gets 5 of those 7
points. The stems are worth the remaining 2 on exact labels, and rather more on
roots (92.1% → 95.2%, which is a 39% cut in root errors; the best root-focused
variant reaches 96.6%).

That distinction has real consequences, because the two changes cost wildly
different amounts:

- Better cleanup is a contained change to `_template_chords` in `src/vgt/chords.py`,
  running on audio vgt already has. No credits, no new dependencies.
- Stems cost LALAL credits and require re-ordering the analysis — chords are
  currently detected *before* separation runs.

---

## Part 3 — Setup and validity

- Song: `The Seven Rivers (Full March - 3_00)` — 178s, 355-beat grid, key A# minor.
- Ground truth: the human-verified chord sheet in the sidecar
  (`analysis.chords.value`, 59 segments, `human_verified: true`).
- Backend: the always-available librosa chroma + maj/min template classifier
  described in Part 1. madmom and Chordino are not installed here, so this is
  exactly the code path that produced your shipped detection.
- Metric: per-beat agreement over the 354 beat intervals of the shared grid.
- **Sanity check passed:** re-running `detect_chords` on the original reproduced
  the stored detection bit-for-bit (83.6% / 89.0%), confirming the harness measures
  the real pipeline rather than a reimplementation of it.

---

## Part 4 — Results

### Each source on its own

Every source run through the *shipped* pipeline, one at a time:

| source | exact | root |
|---|---|---|
| original mix (shipped baseline) | 83.6% | 89.0% |
| instrumental | 82.8% | 89.5% |
| **guitar** | 79.1% | **90.1%** |
| backing (no guitar) | 57.1% | 67.5% |
| bass (via triad templates) | 47.7% | 60.5% |

No single stem beats the mix on exact labels. But look at **guitar**: it has the
best root accuracy of *any* single source, while ranking near the bottom on exact
labels. In plain terms — the guitar stem almost always knows *which letter* the
chord is, and often gets major/minor wrong.

That combination is the key result of the whole experiment. A source that's wrong
in a *different way* than the mix is exactly what fusion can exploit; a source
that's merely worse everywhere would be useless. This is why fusion works at all.

`backing` scores poorly for an obvious reason: it is the guitarless practice bed.
With the harmonic instrument removed there is often little chord content left to
recognize.

### Bass is a bad chord detector but an excellent root detector

Running triad templates on a near-monophonic bass line is a category error — you're
asking "which of these 3-note chords is this?" about audio containing one note at a
time. Its mistakes confirm it: the top confusions are `G#→C#` (×25) and `G#→F`
(×25), i.e. mistaking a bass note for the chord a fourth or a fifth away.

Ask the bass the right question instead — *"what is the single loudest pitch in this
beat?"* — and it transforms:

| bass, used as | root accuracy |
|---|---|
| triad template match | 60.5% |
| loudest pitch = the root | **88.1%** |

88.1% on roots, from a source that carries no chord quality information at all,
nearly matching the full mix's 89.0%. Worth remembering — though see "What didn't
work" for why it did not translate into a gain here.

### Region by region

Per-beat exact/root, bucketed by your 9 human-verified sections:

| region | beats | shipped | mix + better decoder | stem fusion |
|---|---|---|---|---|
| Intro 0–16s | 32 | 88 / 97 | 100 / 100 | 97 / 97 |
| Verse 16–48s | 64 | 86 / 95 | 88 / 100 | 86 / 98 |
| Chorus 48–64s | 32 | 88 / 91 | 88 / 88 | 88 / 88 |
| Verse 64–88s | 48 | 77 / 79 | 100 / 100 | 98 / 98 |
| Chorus 88–103s | 31 | 81 / 90 | 87 / 87 | **65** / 87 |
| Verse 103–128s | 49 | 88 / 88 | 84 / 84 | 98 / 100 |
| Chorus 128–144s | 32 | 88 / 88 | 88 / 88 | 88 / 88 |
| Interlude 144–163s | 39 | 79 / 87 | 90 / 100 | 95 / 95 |
| Outro 163–178s | 27 | 78 / 85 | 70 / 70 | 100 / 100 |
| **TOTAL** | **354** | **83.6 / 89.0** | **88.7 / 92.1** | **90.7 / 95.2** |

**Where fusion wins:** Outro (78→100), Verse 64–88s (77→98), Verse 103–128s
(88→98), Interlude (79→95). These are the sparser, guitar-led passages — exactly
where isolating the harmonic instrument removes the most masking from vocals and
drums.

**Where it loses:** Chorus 88–103s drops 81→65. Root accuracy there holds at 87%,
so every one of those new errors is a major/minor flip, not a wrong letter. That
chorus is the densest, most vocal-heavy passage in the song — precisely where
separation artifacts are worst, and where the stems are least trustworthy. Note
also that all three choruses are the weakest regions for *every* method including
the shipped one, so this song's choruses are simply hard.

### What's left wrong

Ten error runs remain in the best configuration (33 of 354 beats), and they're
musically coherent rather than random:

- `A#:min` → `F#:maj`, three times, 4 beats each. These two chords share two of
  their three notes (A# and C#) — the templates differ only in F vs F#. A genuinely
  hard call for this method.
- `D#:maj` → `D#:min` (8 beats) and `G#:min` → `G#:maj` (8 beats) — right root,
  wrong third.
- Four isolated single-beat flips at segment boundaries.

**A caution on all ten.** "Wrong" here means "disagrees with the human-verified
sheet" — and that sheet is human-verified, not infallible. Hearing a held `D#` as
major rather than minor, or writing `A#:min` where the guitar is actually playing
`F#:maj` over an A# bass note, are exactly the kinds of calls a person gets wrong
too, and they account for most of the list above. Some fraction of these ten runs
may well be the detector being right and the sheet being wrong. Before treating any
individual error as a defect to fix, listen to that passage; and treat the headline
accuracy figures as *agreement with a good human transcription*, not as distance
from ground truth in the absolute sense.

**Seven of ten are quality errors on a correct root.** The bottleneck has moved:
further gains need a smarter classifier (madmom's neural recognizer, or Chordino,
or a key-aware major/minor prior), not more or better stems.

---

## Part 5 — What didn't work

- **Bass as a root prior.** Despite bass being an 88.1% root oracle on its own,
  nudging the fusion toward the bass's root never beat plain fusion — at any
  strength (0.05–0.40), or when applied only to beats where the fusion was unsure.
  The reason: where bass is right, the fusion already agrees; where the fusion is
  wrong, the bass is usually wrong too. The two signals are *correlated*, not
  complementary — the opposite of the guitar result above.
- **Confidence weighting.** Letting each source's vote count more when that source
  is more certain: no better than a plain unweighted sum (88.4% vs 87.6%), and it
  collapses if you lean on it hard (75%, then 40%).
- **Other bar sizes and alignments.** 4 beats starting on the downbeat is a sharp
  optimum: 2-beat bars give 82.5%, 8-beat 74.0%, and starting one beat late costs
  ~18 points. It works because it's aligned to the detected downbeat *and* this
  song happens to hold one chord per bar. Do not assume it transfers.
- **Stems without the original.** Fusing only stems and dropping the mix scores
  84.5%. The original mix remains the single most valuable source — the stems
  supplement it, they don't replace it.

---

## Part 6 — Caveat, and please take it seriously

**One song, 354 beats, and the settings were tuned against the same chord sheet
they're scored on.** The bar size (4) and duration-prior strength (0.8) were chosen
by trying values and keeping what scored best on *this* song — so the exact
percentages are optimistic, in the way that any number chosen to look good is.

What I'd still stand behind, because the margins are wide and there's a mechanism
explaining each:

- fusion beats mix alone;
- bar aggregation + duration prior beats the majority filter;
- guitar carries unusually strong root information;
- choruses are the hard part.

What I would **not** ship without checking against more songs: the specific tuned
constants, and any number to the tenth of a percent. Also worth stating plainly —
the ground truth here is human-verified but, as you noted yourself, not guaranteed
correct, so a few of the "errors" above may be the detector being right.

---

## Part 7 — Suggested follow-ups, in order

1. **Fix the decoder first — it's free.** Replacing the 5-beat majority filter in
   `_template_chords` with bar aggregation + a duration prior gains ~5 points on
   the mix alone, needs no stems, costs no LALAL credits, and touches one function.
   By far the best value-to-risk ratio available here.
2. **Then add multi-source fusion**, as a summed score over
   `original + instrumental + guitar + backing`, gated on stems actually being
   present. Note the ordering problem this creates: chord detection currently runs
   *before* separation, so this needs either a re-analysis pass after Phase 2
   completes or a change to the stage order.
3. **Validate on a second and third song before tuning any constant** — especially
   the 4-beat assumption, which should be re-checked against a song whose chords
   change faster than once per bar, and against a song where the detected downbeat
   is shakier.
4. **Then attack quality, not roots.** Seven of the ten remaining error runs are
   right-letter/wrong-flavor. Installing madmom's neural chord recognizer, or
   adding a key-aware major/minor prior (we already detect the key: A# minor at
   0.908 confidence), likely beats any further work on sources.

---

## Reproducing

The experiment scripts were exploratory and are not committed. The recipe is short:
cache a per-beat chroma for each source over the sidecar's existing beat grid, sum
the 24 template match scores across sources, decode with your chosen cleanup step,
and score per beat against `analysis.chords.value`, bucketed by
`analysis.sections.value`.
