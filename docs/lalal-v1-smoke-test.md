# Manual LALAL API v1 smoke test

This is intentionally opt-in and is never part of CI. An account owner may set
`LALAL_LICENSE_KEY` in their shell, copy a tiny disposable WAV into a temporary
REAPER project, and run the separation command once. Confirm the five v1
operations finish and that the sidecar contains no license value. Afterwards,
delete the uploaded source through the backend's v1 delete endpoint or allow
its 24-hour expiry. Never paste a key into a command, issue, fixture, log, or
sidecar.
