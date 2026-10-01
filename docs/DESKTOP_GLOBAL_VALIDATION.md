# Desktop Global integration validation, 2026-10-01

This continuation starts from published combined source f4ef05ded45a2391be8715db2cec64a22fcbdd9a. Its Global menu, owned supervisor, native preflight and exact config-confirmation flow are integrated. The six original worker bindings, per-child JOIN/receipt protocol, tool loop, and published source artifacts remain preserved.

## Checks completed

The integrated focused run covered 187 tests: 178 passed and nine real tomlkit tests were skipped. Three additional reviewer-derived desktop regressions were then added and passed in an 11-test desktop suite. Distinct focused coverage is therefore 190 tests: 181 executed successfully, nine skipped. This is a focused regression, not a new claim that every unchanged historical suite was rerun.

Covered modules: global gateway, signed queue/native bridge, config safety, pilot proof, desktop lifecycle, unified launcher/environment, and existing Router startup. Eight independent offline review probes passed for crash recovery, concurrent initial start/stop, executable symlink retargeting, expired-proof timeout and edits during config confirmation.

Gate tests use real local gateway/facade/CAS worker code with explicitly synthetic native-shaped queue/receipt ports. Binary version observations and parser bindings are stubbed in gate tests; this does not substitute for real parser or Mac acceptance. Native admission is never performed by Python.

The pilot proof checks an authenticated live bound gateway, exact activation/catalog, current native controller epoch/lease, signed ready queue, unique pinned child, successful request digest/backend response and exact nonce output. It binds the package, installed parser and both observed client binaries; the apply transaction rechecks these plus the before/after config hashes immediately before replacement. Production readiness remains false. Offline fixture mode cannot pass the gate.

Explicit preflight refresh uses the same route/pin/native task and cumulative history, rejecting uncertain or branched earlier requests. Stop intent cancels a first start even before its current-state pointer exists. Config transactions can be found and restored after replacement even if the UI success marker was never written.

## Acceptance still required

- Authorized tomlkit 0.13.3 installation and all nine real-parser tests, including owned comments/trivia conflicts
- Real Mac launcher startup, private environment, installed terminal/bundled desktop binaries and shared CODEX_HOME
- Authorized live Google queue and per-thread raw-file/Docs probes
- Actual active GLOBAL native controller and selected children, within platform slot/latency limits
- Real native preflight followed by explicit exact-diff consent
- Full desktop restart and an observed new desktop-thread route, plus a separately started terminal route
- Real tool round trips and stop/restore across interruption

No real user settings, authentication, Google resources, dependency installation, native inference or Git push occurred during this integration. Local stop does not establish native task termination. A selected bundled executable and CODEX_HOME do not alone prove the running desktop uses that home/profile.
