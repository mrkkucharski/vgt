# 7Rivers strum-onset reference

Numbers-only Phase 0 reference for strumming detection. It is derived from the
human-created `strum reference` MIDI track in `test/7Rivers/7Rivers.RPP`, with
the 4.0 s project position of `guitar.wav` subtracted. Each value is the leading
edge of one hand stroke, not one note per string.

The reference covers one continuous 23.55 s window (30 strokes). It is not
quantized: its purpose is to measure detector timing. The track was aligned to
the guitar stem waveform by the maintainer; this fixture contains no audio and
does not attempt to encode direction or velocity.

`scripts/strum_detection_probe.py` treats `window_s` as authoritative: a
prediction outside that span is excluded, and an omitted real stroke inside it
counts as a false negative. Do not extend the list with an estimator-derived
onset; revise it only from a human annotation.
