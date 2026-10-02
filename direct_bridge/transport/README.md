# Durable direct transport: offline prototype

This is an isolated standard-library experiment. It does not change the frozen
Dots2Codex package, user configuration, credentials or live activations. It
executes no model, native child or Mac command. It is **not an installed MCP
connector**, not a production server and not a deployment.

## Run

From the `dots2codex-direct-prototype` directory, Python 3.10+:

```sh
python -m unittest transport.test_queue -v
python -m transport.benchmark --turns 40 --output transport/benchmark-results.json
```

No dependencies need installing. The benchmark starts a bounded-lifetime HTTP
fixture on `127.0.0.1` with an ephemeral port and cleans up in `finally`. It sends
only synthetic fixture data. It performs no external networking. Its two paths
use **hard-coded fake identities, not authentication**: never expose or tunnel
this benchmark server. The actual MCP/tunnel adapter is owned by `mcp_adapter/`.

## Small contract

`Queue(path)` persists one SQLite database. `install_route(RouteAuthorization)`
is trusted local setup, never a remotely exposed method. It pins these fields:

- Binding: grant_id, route_id, session_id, thread_id, model, reasoning_effort
- Exact client actor and one native worker actor
- Explicit approval reference and an authorization time window

A route cannot silently change identity, model, effort, child or grant. New
routes need separate explicit authorization. `Principal` is a **logical identity
asserted by the trusted adapter**, not cryptographic proof of platform identity.
A caller-provided actor ID or `approval_ref` string cannot establish authority.

Client methods:

| Method | Purpose |
| --- | --- |
| `enqueue_request(principal, binding, request_id, seq, payload, previous_result_id=None, schema_refs=None)` | Persist one immutable request; replay identical ID/body safely |
| `put_schema(principal, binding, name, schema)` | Persist one immutable schema and return its SHA-256 |
| `get_result(principal, binding, request_id)` | Read terminal result or explicit pending/cancelled state |
| `cancel_request(principal, binding, request_id)` | Cancel before admission, or request cancellation of in-flight admission |
| `revoke_route(principal, binding)` | Stop all new route work and cancel pending work |

Conceptual native tool surface (schemas in `surface.py`):

| Method | Purpose |
| --- | --- |
| `claim_request(principal, binding, request_id)` | Acquire or reread the same bound request payload |
| `read_schema(principal, binding, request_id, name, sha256)` | Fetch only an exact immutable schema referenced by that request |
| `submit_result(principal, binding, request_id, result)` | Save result and terminal state atomically, with identical retry support |

`reserve_execution(principal, binding, request_id)` is a separate trusted runtime
hook. It returns `execute: true` exactly once per request, committed before the
caller may admit native execution. It is intentionally not an additional
model-facing tool in the conceptual three-tool surface. The integrated facade
may expose a different static tool set appropriate for its native action loop.

The host adapter injects the pinned principal and binding separately from JSON
arguments. `ToolSurface` rejects principal/binding overrides in wire arguments.
`JsonLoopback` models that serialization contract; it does not open sockets.

## Replay, continuation and cancellation

1. The client durably chooses one stable request ID **before its first send**.
   Retry the same ID and body after network failure, never invent a new ID.
2. A route accepts contiguous sequence numbers, one unsettled turn at a time.
   Turn N+1 must acknowledge turn N's exact result ID, including cancellation
   tombstones. IDs plus full binding/body digest prevent mismatched replay.
3. `claim_request` is repeatable acquisition. Losing its HTTP reply does not
   consume execution permission: the same authenticated native actor can reread.
4. The trusted native adapter consumes `reserve_execution` immediately before
   actual native admission. Duplicate reservations never grant execution again.
   If its reply or native admission response is lost, inspect/reconcile that
   **same actual native child**. The gate never resets, times out or transfers.
5. Retried `submit_result` is accepted only for identical canonical content.
   A different result is rejected. Payload and completion are one SQLite commit;
   failure leaves both pending. Read/reconnect returns the saved result.
6. Cancel before execution reservation, even after claim, makes a terminal
   cancellation immediately. Cancel after reservation is cooperative: stop
   further execution at a safe boundary, then submit the original late result or
   cancellation acknowledgement. Settlement is saved but output is suppressed.
   An unsettled cancelled admission cannot be bypassed by enqueueing another turn.
7. Grant expiry blocks new acquisition, schema access and admission. It still
   permits already-admitted result settlement and status reading. Expiry is not
   cancellation; use cancel/revoke for output suppression.
8. No timeout is treated as execution failure. There is no lease, heartbeat,
   automatic reassignment, inference API fallback or Mac-side command executor.

This is **at-most-once admission**, not exactly-once distributed side effects.
There is an unavoidable crash window between durable permit reservation and an
external native admission/tool effect. Unknown admission remains paused until
actual native state can be reconciled. If state cannot be proven, it must not be
executed again automatically. If an adapter blindly acts on a replayed payload,
this storage module cannot protect it.

A Mac Responses facade also needs its own stable action-ID correlation and
one-use emission/consumer fence for function/custom calls. A result can be read
many times; repeated reads are not permission to rerun its tools. The integrated
facade/orchestration layer handles that boundary. The queue does not execute the
Mac command itself.

## Runtime and permission hooks before real use

- Verify explicit user approval covering the chosen native route/model and scope.
  Neither queue data nor model-generated instructions can expand that scope.
- Use only the officially authorized localhost MCP + Secure MCP Tunnel route.
  Existing install/tunnel discovery and any new persistent access require their
  actual permission workflow. This prototype creates none of it.
- The real adapter must authenticate the transport owner/session and pin the
  logical actor and binding in trusted configuration; do not accept them in tool
  arguments or invent signed native identity claims. Preserve ordinary tool
  confirmation requirements independently of request transport.
- Admit/continue the actual dot native child with the authorized model/effort.
  Capture actual admission identity. Do not replace it with an API model call,
  cloud-thread imitation or a duplicate Mac tool executor.
- Prove real MCP discovery plus a harmless call in both root and actual native
  child before claiming the tool is reachable. Neither local unit tests nor
  stdio protocol tests prove this gate.
- Route opaque context full/delta payloads into the context module. Keep exact
  schemas content-addressed and request-bound. Never reconstitute huge static
  control instructions in every turn.
- Store the DB in a host-created private local directory with owner-only access;
  use a trusted clock and reliable durable filesystem. The queue detects clock
  rollback relative to successful active operations. It is not a secure clock.
- Use bounded body parsing, sanitized errors, application-level storage quotas,
  shutdown/cancellation handling and production HTTP/MCP framing in the real
  adapter. This module limits individual request/result/schema size to 1 MiB,
  128 turns per route, and 1,024 schema references per request. It has no automatic
  garbage collection or aggregate storage quota. Do not use network filesystems
  without separately validating SQLite locking/durability.

## Evidence and limits

The focused tests cover contiguous multi-turn acknowledgement; acquisition reply
loss; reconnect after execution reservation; duplicate/concurrent reservations;
immutable request/result IDs; atomic failed-save rollback; route/model/actor
mismatch; exact schema lookup; cancellation before/after reservation; revocation;
expiry; clock rollback; and rejected authority injection.

`benchmark-results.json` measures 40 synthetic turns for ~1 KiB and ~515 KiB
requests. Each roundtrip includes four actual local HTTP/JSON calls, SQLite FULL
commits, and the separate internal execution permit. It excludes model latency,
TLS, official tunnel, remote network, real MCP, real Mac/Codex and tool execution.
Local millisecond transport results are **not an end-to-end latency promise**.
