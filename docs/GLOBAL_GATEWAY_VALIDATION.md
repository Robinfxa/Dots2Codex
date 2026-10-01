# Global gateway validation, 2026-10-01

> Historical isolated-gateway snapshot. The Global menu and explicit trial are now integrated; the current normal path is config-first `client-config-trial/1`. See [current activation](DESKTOP_GLOBAL.zh-CN.md) and [integration validation](DESKTOP_GLOBAL_VALIDATION.md). Counts and unsupported-menu statements below describe the older snapshot, not the current source.

## Gate

**Pass for an honestly labelled offline-integrated preview. Not production global takeover.**

`production_ready=false` and `ready_for_config=false` remain unconditional in this release. There is no supported switch to bypass those gates. The unified first-launch launcher is now in the same source package; its global menu remains unsupported. Seamless UI/global configuration integration and live Mac acceptance remain future work. The combined-package regression is documented in [EXPERIMENTAL_PREVIEW_VALIDATION.md](EXPERIMENTAL_PREVIEW_VALIDATION.md); counts and unchanged-base claims below describe the original isolated gateway candidate.

No real user global config/auth, OAuth, Google resource, native inference worker, install, active runtime, or Git push was changed by this implementation/validation. The optional tomlkit dependency was not installed; genuine parser integration remains unverified.

## Results

- All 525 pre-existing Python tests passed, using system Python for ordinary suites and the already-installed, read-only SDK venv for the three Google SDK mock tests
- Final new focused suite: 64 tests, 55 passed and 9 skipped
- Combined distinct Python coverage: 580 passed, 9 skipped
- Existing Node runner + adapter suite: 25 passed
- All 178 published base files retain their exact original bytes and modes
- Offline demo: two different model/effort threads, two distinct pins/children; zero native inference calls and zero Google writes

The full regression matrix ran before the final immediate-follow-up regression was added; the final focused suite was rerun against the final gateway implementation. The base files remain byte-identical. Counts above include each distinct test once.

Full matrix groups: tests 134; remote_tests 319 at that run (9 skipped); CAS audit 47; Drive audit 48; Google setup fixtures 37; SDK mock tests 3. The final focused run adds the one new handoff regression to the full discovered set. No test failure remains attributable to product code. An initial attempt to run ordinary tests with the SDK-only venv failed because it lacks jsonschema; those suites were rerun successfully using the existing system interpreter. Nothing was installed to resolve this.

## What the new tests establish

- Fixed-port lease, unrelated port occupant rejection, authenticated challenge/readiness and private state
- Canonical session/thread isolation, valid selection before binding, immutable per-thread model/effort and generation fencing
- Two concurrent clients with different selected pairs; distinct native-plan identities, immutable pins, journals and CAS controls
- Strict authenticated Docs queue projection, exact revision CAS, tamper/rollback rejection and source binding
- Active native helper join/heartbeat/claim/begin/result workflow, one-attempt native reservation across helper restart, and actual-argument mismatch rejection
- Closed queue, stale heartbeat, expired controller/child bootstrap and saturated capacity reject new native admission
- Existing v3 child JOIN, raw probes, bundle/polling proof and real local Responses facades connected end to end using fake Google/native ports
- Complete function-tool loop, correlated output and durable delivery receipt; repeated tool intent and uncertain side effect never replay
- Pure-text retry reuses the result; request-dispatch intent survives gateway restart
- First request waits through isolated admission and executes once without user resubmission when readiness arrives within budget
- Parsed JSON progress events and one stable outer Responses ID; original backend ID retained privately; tool item/call identifiers unchanged
- Admission timeout and pre-dispatch disconnect do not later execute the abandoned prompt
- Connection-close delayed-body regression obeys the post-dispatch absolute deadline
- Immediate full-history follow-up waits through the prior completed response's durable journal flush
- Ready-route transport restart preserves native task, pin and history; no new native spawn or queue epoch
- Durable local stop cannot be undone by a stale heartbeat; authoritative control close still runs without an in-memory facade
- Config snapshot ownership/type/symlink/hardlink guards; missing optional dependency fails closed
- Explicit config confirmation, production readiness gate, compare-before-replace, concurrent edit preservation, crash reconciliation, backup hash validation, exact byte restore, and absent-versus-empty originals

Independent review additionally checked a forged port server, same-session-ID cross-thread tool history, queue lifetime gates, tool-loop recovery, post-bootstrap-window transport reattachment and the stalled-body deadline. Those checks used temporary fixtures only.

## Nine skipped real-parser tests

Tomlkit 0.13.3 is not available. The parser integration tests are deliberately skipped, not replaced by a fake parser. They cover:

1. CRLF, comments, quoted provider IDs, inline tables and Unicode
2. Three-way restore preserving unrelated new values/comments
3. Conflicts when an owned value changes
4. Malformed TOML and unsupported provider structures
5. Stale preview and concurrent writer during apply
6. Repeated apply and transaction reconciliation
7. Other providers, legacy profiles and safety settings
8. User-added comments beside an owned scalar
9. User-added comments inside the owned provider table

The last two must preserve the comment or safely conflict; they must never silently discard it. This has not yet been verified with the real parser. The production config gate cannot open on the strength of the stdlib-only tests.

## Remaining acceptance

- The active agent actually calling the supported native tool with the selected model/effort and real returned task identity
- Connected Google SDK/connector resource access, creation, CAS and raw probes; live latency versus the bounded heartbeat window
- Current normal flow: exact terminal catalog adapter and selected safe CODEX_HOME, full consumer/app-server/managed-daemon restart and fresh post-commit routes; old/resumed threads and project/managed/profile/CLI overrides remain uncertain. The older strict profile separately checks the desktop bundled CLI only when explicitly selected
- Real function/custom-tool effects and recovery after disconnect, Mac sleep/reboot and native-controller exit
- Optional parser dependency approval, genuine format-preserving tests and review of the exact proposed user config diff
- Integration with the separately delivered unified Mac launcher, Finder/app behavior and signing/quarantine

The local threat model does not protect against malicious processes running as the same Unix user. Generation and thread headers are isolation labels, not authentication secrets. Signed local control, rejected browser origins and private files do not constitute OS isolation.
