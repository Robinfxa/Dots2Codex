# Heartbeat v2 recovery candidate

Base: published `90c9c071b9fb02c1fa9b13688386b9631e53e2da`, source tree `71b0c597c50278995f7f958dafacd3b54dd81309375a25a21d344f5cc8df1d6e`.

## Reviewed change

- Signed immutable timing: exactly 900 seconds (15 minutes) of controller freshness for fresh activations, target heartbeat cadence 25 seconds; root and controller lease lifetime unchanged. Old signed 180-second roots fail closed without migration or runtime rewriting
- Protocol-v2 queue root, marker, MAC domain, native ledger, CAS plan and Google mirror mode; source binding includes timing, gateway, pilot and complete cell driver
- Complete generated JOIN cell immediately verifies its first heartbeat before it returns; JOIN-only status cannot start preflight or admit a client
- Controller events, native dispatch, local gateway and pilot use the same strict freshness predicate; first heartbeat, root, epoch, generation and child capacity remain fenced
- Prepared CAS acceptance is bounded by preparation +120 seconds, predecessor heartbeat expiry, lease and root expiry. A newer signed timestamp is not proof of provider commit time
- Durable native/Mac/gateway stale/clock-rollback fences prevent later snapshots reviving the same controller. Gateway clock observations do not refresh heartbeat time
- Unresolved CAS permits only exact read-only reconciliation; unknown native spawn remains one-attempt. Prepared spawn authorization lasts at most 10 seconds
- Closed/expired/failed/stopped or unsupervised activations now require new activation/JOIN rather than suggesting same-activation resume

## Historical offline evidence (180-second policy)

The final focused queue, timing, pilot and desktop run passed 71 tests. This includes 20 fake-clock heartbeat tests for 26-second JOIN readback, 45-second CAS, 90-second native result, exact-180 expiry, prepared tick delivery after expiry, expired lease, clock rollback, closed queue, capacity, generation, source/policy tamper, unknown CAS, unresolved-heartbeat blocking of spawn exposure/dispatch, and no replacement spawn. Pilot tests reject an alternate correctly signed queue root. The standalone cell sequencing suite passed 4 tests.

These historical counts do not attest the new 900-second policy. Its final offline results belong in the current package manifest and validation receipt. The unchanged limits remain a 120-second prepared-CAS budget and 10-second native dispatch authorization.

An independent runtime harness exercised the complete emitted cell source, the real Python helper/capture/chunk CLIs and synthetic connected tools. It checked JOIN26/HB26, JOIN90/HB0, 26 seconds per read, applied-but-lost CAS response reconciliation, unapplied unknown write and an over-budget JOIN90/HB90. No real Google/native inference, OAuth or user configuration changes were performed. Additional final aggregate results are recorded in the package manifest and external validation receipt, not implied by focused tests.

## Fresh restart

1. Preserve the failed activation, queue and attempt journals. Stop/close it through the normal authorized workflow and report uncertain stop evidence accurately
2. Install/use the same reviewed v2 package on the Mac endpoint and active native controller. Do not patch an old signed root or loosen its saved lease
3. Use a fresh gateway/bridge/controller state and a new bounded activation with new Doc/code. This requires the user's existing authorization for that workflow; status inspection creates nothing
4. Send the newly generated private `DOTS2CODEX_GLOBAL_JOIN_V2`. Emit/execute the complete JOIN cell as documented in `GLOBAL_NATIVE_CONTROLLER.md`. Verify JOIN plus first heartbeat before reporting active
5. Keep the active agent supervising with one-shot heartbeat cells; schedule relative to signed heartbeat time, not the end of a slow cell. Dispatch native children only from verified begin and current one-use plans. Pre-emit the next one-shot heartbeat before the final dispatch sequence and execute it after successful spawn, before lengthy capture/record; never bypass unresolved CAS or replay spawn
6. Verify the real Mac preflight, then obtain the separate exact-diff confirmation for any global configuration write. A production-ready claim remains prohibited

## Historical live limits of the heartbeat-only repair

The limits below describe that earlier heartbeat-only candidate. The current response-budget change is documented in `DESKTOP_GLOBAL.zh-CN.md`; it does not extend any already-issued plan or signed authority.

In that heartbeat-only candidate, first-request admission and desktop preflight UI waits were 180 seconds. Initial supervisor-stage wait was 90 seconds, child bootstrap normally 600 seconds, preflight plan 600 seconds and sealed proof 300 seconds. Native JOIN/preflight taking several minutes could still time out. That heartbeat repair did not extend response deadlines, activation life or leases. Neither it nor the current response-budget change certifies real Mac/client compatibility, proves native children stopped, or proves underlying model identity. Real acceptance is still required.
