# Manual LALAL API v1 smoke test

This is intentionally opt-in and is never part of CI. An account owner may set
`LALAL_LICENSE_KEY` in their shell, copy a tiny disposable WAV into a temporary
REAPER project, and run the separation command once. Confirm the five v1
operations finish and that the sidecar contains no license value. Afterwards,
delete the uploaded source through the backend's v1 delete endpoint or allow
its 24-hour expiry. Never paste a key into a command, issue, fixture, log, or
sidecar.

## Recorded acceptance run — 2026-07-20

An account owner completed this opt-in check with a 10-second excerpt made from
the repository's owned audio fixture. The disposable project, audio, generated
sidecar, and stems stayed outside the repository and were not committed.

- The environment-only credential authenticated a real LALAL API **v1** run.
- Free preflight quoted **five** outstanding operations and a duration-based
  estimate of **0.83 minutes** before any split was submitted.
- Upload, preflight, split submission, polling/check, and WAV download all
  completed for `vocals-original`, `bass-original`, `drums-original`,
  `guitar-original`, and the `guitar-instrumental` cascade.
- The result contained the expected six artifacts: vocals, instrumental, bass,
  drums, guitar, and backing/no-guitar. Each was a readable, non-empty,
  44.1 kHz stereo WAV of 10.0 seconds and matched its recorded byte size.
- A byte-level check confirmed that neither the license value nor the
  `LALAL_LICENSE_KEY` variable name appeared in the disposable sidecar or
  captured progress log.
- The two uploaded v1 source objects (original and cascade input) were deleted
  through the backend's `/api/v1/delete/` endpoint after validation.
