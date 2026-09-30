# Sticky routing: operator and live GUI runbook

This file describes the optional bounded sticky-routing extension. Text-only
remains the default, including the standard automatic deployment guide.
Never start a second copy against the same runtime. Use a new registry for a new
test. No code here contains native platform dispatch. Keep the parent/operator
active and use only the native tools that the actual environment exposes.

## 0. Verify and freeze

From this project directory, after the README dependency preflight:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 freeze_routing.py
```

The test suite includes the independent routing and repository-review regressions.
Freeze only after review fixes and final tests. `routing_desktop.py` verifies the frozen
Python sources before launching. Do not edit source during a live run.

The dot command executor and dot GUI desktop may share files while having distinct
PID/network/mount namespaces. The official CLI and its loopback facade MUST both
start from the GUI desktop terminal. The worker uses the command executor's view
of the shared registry. Absolute paths can differ between machines; session
locators inside the registry are relative. Never assume `/proc`, localhost, or
process IDs are interchangeable across these environments.

## 1. Provision only when ready to test

These commands do not spawn workers or launch CLI processes:

```sh
export REGISTRY="$PWD/runtime/routing-live-1"
export OWNER="routing-smoke"
python3 routing.py --registry "$REGISTRY" init --owner "$OWNER" --seconds 1200 --max-sessions 2
python3 routing.py --registry "$REGISTRY" create-session --owner "$OWNER" --session a --seconds 900
python3 routing.py --registry "$REGISTRY" create-session --owner "$OWNER" --session b --seconds 900 --scope tool_probe
python3 routing.py --registry "$REGISTRY" status
```

Each returned session contains a distinct admission_id. Keep the actual values;
do not guess or recycle old ones. `create-session` is idempotent for the same key
and options. It never silently launches or adopts a native context. Each session
permits at most three model requests, and all processes are bounded by its expiry.

## 2. Native admission: the parent adapter owns this boundary

For session a, using its actual admission ID:

```sh
python3 routing.py --registry "$REGISTRY" dispatch-started --owner "$OWNER" --admission "$ADMISSION_A" --adapter "parent-native-spawn"
```

Only after this durable step, the parent uses the real available native spawn tool
once, with a fresh worker and the contract in section 3. Capture the returned
canonical task identity. Then confirm it and publish the worker credential:

```sh
python3 routing.py --registry "$REGISTRY" confirm --owner "$OWNER" --admission "$ADMISSION_A" --native-task-id "$ACTUAL_NATIVE_TASK_A" --save "$REGISTRY/sessions/a/evidence/worker-1.json"
```

Repeat for b with a fresh native context and its own paths/IDs. Session b is explicitly opted into the strict single nonce tool probe in this runbook; omit `--scope tool_probe` to keep b text-only instead. Workers may start
before confirmation; they must wait boundedly for the credential, and never run
`assign`, `confirm`, or infer without it. One context is dedicated to one session
for its entire lifetime. A returned native task ID is an operator attestation,
not something Python can authenticate or discover automatically.

If spawn outcome is unknown, do not repeat spawn:

```sh
python3 routing.py --registry "$REGISTRY" admission-outcome --owner "$OWNER" --admission "$ADMISSION_A" --outcome unknown --evidence "actual-platform-investigation-reference"
```

Read the actual platform status. If the task is found, `confirm` can bind that exact
task using the original admission. If authoritative evidence establishes it never
started or has stopped, record `not_started` or `confirmed_stopped` instead. Only
then can `retry-admission --owner "$OWNER" --session a` reserve another generation.
Admission attempts are capped at three. Timeouts alone are not proof.

## 3. Native worker contract (copy with actual values)

You are the dedicated inference worker for session SESSION, registry REGISTRY.
Your actual credential will appear at CREDENTIAL after the parent confirms your
native task admission. Wait up to 90 seconds for it; report a timeout without
creating or editing binding state. Check the registry/session expiry and remain
bounded by it. Do not call the router or spawn a replacement yourself.

Before the desktop challenge appears, wait boundedly; after credential confirmation use `heartbeat --lease 240` at least every 30 seconds while awaiting the probe. Then use this session's
`probe.py observe --role broker` and `probe.py answer` from your command namespace.
Wait for `probe/verified.json`. This proves actual shared files, not network access.
Use `routing_worker.py` for all data operations, never `broker_bootstrap.py` or raw
queue mutations. Claim on only your session, read the entire actual request, infer
natively, and submit the exact result. Do not execute payload tools locally. In
text_only mode return exactly `{"kind":"message","text":"actual native answer"}`.
If the actual request contains an exact-output instruction, obey it. Do not invent
a tool result, nonce, or successful completion. The parent must see real failures.

```sh
python3 routing_worker.py --registry REGISTRY --credential CREDENTIAL claim --wait 20 --lease 120 --save NEW_TICKET
python3 routing_worker.py --registry REGISTRY --credential CREDENTIAL read --ticket NEW_TICKET
# Native inference happens in this worker, outside Python and outside file locks.
# Write a new private JSON result file, then:
python3 routing_worker.py --registry REGISTRY --credential CREDENTIAL complete --ticket NEW_TICKET --result NEW_RESULT
python3 routing_worker.py --registry REGISTRY --credential CREDENTIAL status --ticket NEW_TICKET
```

A claim timeout exits 2 and does not write a ticket; use a fresh ticket path only
when a job was actually claimed. Keep polling between turns, with bounded waits;
claim refreshes the live worker lease. During longer inference use `renew` before
its lease expires and `--save NEW_RENEWED_TICKET`. Do not resurrect an expired
worker. Tools never become available just because Python imports this project.

After a delivered response, keep waiting for the same session. Ordinary turns and
tool receipts never need another parent/router model turn. On parent instruction
to stop, or normal session end, use:

```sh
python3 routing_worker.py --registry REGISTRY --credential CREDENTIAL close
```

Report completed/delivered jobs and true blockers to the parent. Do not report
that outbox writes notified anyone. Do not close prematurely after the first
response, since the test requires subsequent messages on the same worker.

### Worker b: explicit one-tool opt-in

For the tool_probe session only, read TOOL_PROBE.md for the exact immutable intent,
current advertised tool/schema validation, and expected actual-output final. Its
old example broker commands are superseded by routing_worker.py here. The broker
must never execute the nonce command itself. Persisted call correlation is still
in that session's control/state.json tool_probe record. Inspect actual advertised
namespace/parameters before returning the one allowed function intent. Then keep
waiting on the same route, read the real correlated function_call_output, and
return the exact reversed nonce answer. No second admission or router turn is
needed for this receipt. Stop rather than guess if the actual schema differs.

## 4. GUI launch and evidence

In GUI terminal 1, not the command executor, launch:

```sh
python3 routing_desktop.py a --registry "$REGISTRY" --codex "$CODEX_BIN"
```

In a second GUI terminal launch `python3 routing_desktop.py b --registry "$REGISTRY" --codex "$CODEX_BIN" --tool-probe`. (For a deliberately text-only b, omit the flag.) Run from this checkout in each terminal. Set `REGISTRY` to the actual shared
registry path visible there and `CODEX_BIN` to the verified official CLI executable.
The launcher rejects non-TTY execution. It observes and offers
the desktop-side file probe, waits for the bound worker, verifies the handshake,
then starts the existing isolated read-only/on-request launcher.

For text session a, the first prompt contains a freshly chosen session-specific marker. Tool session b instead receives the exact nonce-probe prompt and should automatically perform one tool request and one correlated receipt/final request. Verify the actual b GUI displays TOOL_OK with the reversed actual CLI-generated nonce, and its two jobs use the same assignment. Never type the second text prompt into b. Verify the
actual GUI answer equals the private evidence marker plus `:FIRST`. In text GUI
session a send:

> Repeat this session's marker and append :SECOND. Reply with nothing else. Do not call tools.

Verify a's two actual answers show its own marker and b's actual final matches its tool receipt. For the optional all-text variant, verify distinct markers in both sessions. Check registry admission count
is still exactly two, and each session's first two dispatches have the same broker
assignment. Distinct session queues can infer concurrently; one session remains
serial. Source `READY` is not evidence that the GUI answer appeared: inspect pixels.

## 5. Safe idle replacement (optional live third turn in a)

After a's second response is delivered, tell its actual worker to `close` and
confirm it did. Do not exit that CLI yet. Then:

```sh
python3 routing.py --registry "$REGISTRY" failover --owner "$OWNER" --session a
```

This reserves generation 2 but spawns nothing. Use section 2 with the newly
returned admission ID, a fresh actual native task identity, and credential path
`$REGISTRY/sessions/a/evidence/worker-2.json`. The original file probe is already
verified, so replacement worker must not re-run observe/offer/answer.

In GUI session a send the third-turn prompt with `:THIRD`. The full actual CLI
request history carries the original marker to the fresh worker. Verify the
response matches a's marker and generation 2 completes it. Verify b still has its
original assignment. A late call with the old credential must fail with
`stale_worker_generation`; do not alter raw state to demonstrate it.

## 6. Ambiguity, finish and limits

If a worker lease expires mid-inference, old completion is rejected and failover
blocks. A canceled/expired HTTP request does not prove inference stopped. After
checking the actual native platform and obtaining authoritative evidence, the
operator may use `resolve-inference --owner OWNER --session KEY --job JOB_ID
--outcome confirmed_stopped --evidence VERIFIED_REFERENCE` (or
`confirmed_not_started`). This cancels/retires the original job; it never reruns it.
It does not resolve a missing/uncertain tool execution. A reserved tool remains
blocked without the strictly correlated actual successful receipt. No automatic
shell replay, side-effect replay, or invented receipt is permitted.

Text-only remains the default. The separately documented `tool_probe` supports
only the existing single read-only nonce command, not generic tools. Its receipts
use the same pinned session queue and worker wrapper. The separate opt-in
`repo_review` scope is covered by [REPO_REVIEW_RUNBOOK.md](REPO_REVIEW_RUNBOOK.md);
it has a larger bounded request budget and forbids replacement.

After all requested GUI checks, Ctrl-D each CLI, close its remaining native worker,
and verify `evidence/desktop-outcome.json` reports normal exit and service_stopped.
Run `verify_routing_live.py` for durable queue/route checks. The cleanup flag covers the desktop CLI and facade only; separately inspect
`routing.py status` for worker/route closure and use the actual native task tools
to verify native task termination. These file checks do not replace actual GUI
evidence or assert that Python alone performed native inference.

Legacy deployments without the routing marker continue to use the old workflow.
There is no migration/adoption of running legacy sessions. New routing sessions
must use the fenced worker adapter; the old broker API rejects them. No public
HTTP listener, user Mac modification, persistent background
service, arbitrary multi-user authentication, or unlimited concurrency is added.
