# Latency candidate validation, 2026-09-30

This isolated candidate was tested offline. No Google writes, deployment,
authentication changes, source-runtime hot-switch or live speedup measurement was
performed. The preceding running release was left unchanged.

Separately, the pre-optimization long-session baseline completed a live bounded
four-request Mac-controller/native session, including one function-tool
continuation; see [the public validation record](REMOTE_VALIDATION.md). That
session did not use this parallel executor and provides no live speedup evidence
for it. The session was subsequently closed and its worker stopped.

## Passed checks

- 134 legacy tests, including five release-packaging checks
- 149 remote tests, including the existing 23 long-session cases, 19 new timing
  cases and 11 new batch/input/capture cases; imported test classes cause some
  repeated coverage, so these totals are not independent safety guarantees
- 47 independent CAS audit tests
- 48 independent Drive audit tests
- 3 offline real-Google-SDK tests using the pre-existing optional-dependency
  environment; the default Python lacks httplib2 and cannot run these three
- 25 Node tests: 21 runner faults/scheduling cases and four adapter integration
  cases using real local Python CLI/journals with synthetic native tool responses
- Python compile checks, Node syntax checks and generated-cell syntax check

The adapter integration tests cover parallel fixed immutable uploads, actual
async monotonic clock bridging, exact-ID byte/metadata verification, result CAS,
no batch restart, preserved error-response association, large quote-heavy/null/
Unicode captures, and large helper packets through bounded file-backed chunks.
Python batch tests cover partial failures, duplicate IDs, swapped or mutated raw
evidence, stale/one-shot CAS reservations, and one-use large input exposure.

An independent reviewer also ran 12 adversarial Python cases and exercised a
round-11 synthetic control ledger with over 57 KiB of CAS arguments under a strict
stdout cap. The final adapter needed 18 local helper calls for that control
cycle (down from 58 during development), with one CAS and no dropped reads.
These are additional audit observations, not live Google evidence.

One early full regression run was launched as a background shell job and failed
only the CLI SIGINT shutdown timeout: the shell job inherited an ignored SIGINT.
The same unmodified suite then passed all 149 tests when run normally in the
foreground. Do not background the suite using shell `&` when validating signal
handling. No production code or test timeout was relaxed.

## Controlled deterministic benchmark

Run `node native_connector/benchmark.js` to reproduce the virtual-delay test:

- Serial: 866 virtual milliseconds
- Three concurrent immutable chains: 444 virtual milliseconds
- Difference: 422 virtual milliseconds; ratio: 1.95
- Both: 12 connector RPCs, two fresh control reads, four external writes

The same fake latencies and protocol operations are used in both modes. Only the
schedule changes. This demonstrates overlapping independent waits, not a 1.95×
live improvement. The benchmark excludes native-model boundaries, actual
connector quotas and queues, real I/O/fsync time and the actual adapter's local
helper/clock calls. The real adapter avoids model turns within a cell but adds
local helper calls; large ledgers can still exhaust the 10-second freshness guard
and stop safely. Input extraction/orchestration and provider inference cannot be
separated from prior coarse live timestamps.

## Acceptance limits

Safe protocol compatibility and offline orchestration are tested. Real native
connector tool-shape support, actual wall-clock benefit, resource quotas, long
uptime and user-visible delivery latency still require a separately authorized
fresh live session. `DELIVERED` remains a receipt ACK rather than a UI timestamp.
The candidate is ready for that controlled validation, not automatically deployed.
