# Native fast loop and live acceptance

The executable fixture is `python3 -m orchestration.vertical_slice`. It uses a
real MCP stdio subprocess and real loopback HTTP. Its worker and Mac are explicit
synthetic drivers: no native inference and no Mac tool execution are claimed.

## The loop to try live

1. The authorized controller assigns one logical worker to one conversation and
   starts exactly one real native child. Keep the admitted child and its context
   for successive tool callbacks. The Python bridge never starts a model.
2. `get_request` returns the initial request projection once. Full source requests
   still arrive from Codex and are validated locally; tool schemas are exact,
   request-bound data loaded by `discover_tools` / `lookup_schema` as needed.
3. The native child calls `submit_action_and_wait_result` with its exact Responses
   tool intent and stable action ID. The bridge commits that response, the waiting
   HTTP request receives SSE, and the existing Mac Codex process executes the tool
   under its own sandbox and approval policy.
4. Mac Codex sends its next normal Responses request. The facade validates the
   full history and exact emitted call IDs locally. The still-running MCP call
   returns the matching new output and context delta. The child continues with
   that tool result. Parent polling, a replacement child, Drive/Docs, and full
   model-visible history hydration are absent from the warm path.
5. Repeat in the same child. Finish with `finish_request` containing the native
   final answer. This confirms local commitment; actual HTTP delivery remains a
   separate observation.

The fixture records an operation ledger and demonstrates one full context return,
three deltas, one logical actor binding, one schema lookup, four immutable response
commits, and four HTTP emission starts. Same-epoch unchanged schema receipts stay
valid, so warm callbacks need no repeated discovery/lookup/ack RPC.

## Irreducible crossings and bounds

Each dependent step still needs native reasoning, one native tool invocation,
transport to/from the Mac process, actual Mac execution, and a native continuation
that observes the result. Delayed output can require another `await_result` tool
call after a bounded wait. None of those costs is measured by local fixtures.

- Application waits are 0–5 seconds through the static MCP schema. Adapter request
  handling is independently bounded. Test actual host/tunnel deadlines separately.
- A `pending` return or MCP cancellation is an observation, not execution failure.
  Reuse the same action ID with `await_result`. Never create a replacement action,
  reset a reservation, or interpret timeout as authorization to execute again.
- Generic MCP notifications, WebSockets, server writes, and changed tool-list data
  do not establish a supported way to wake or inject content into an idle model.
  An active tool call can return its own result; an idle child requires a supported
  host continuation action. Keep that boundary explicit.
- Exact schemas returned as JSON are data. They do not install native tools. The
  static eight-tool bridge must actually be installed/exposed by the supported
  host plugin route for both the parent and native child.
- Context receipt chaining is local acknowledgment, not proof of LLM reading,
  retention, or platform context injection. Unknown/restarted/compacted context
  needs explicit rehydration. The current facade deliberately fails closed on a
  process restart with reserved work, rather than silently recreating inference.
- Logical actor IDs pin single-owner routing; they are not cryptographic platform
  attestation. Authenticated user-controlled tunnel/workspace access and a trusted
  controller are the trust boundary. This is not a public multi-user service.

## Minimal actual Mac/native acceptance experiment

No part of this experiment was run by the offline fixture. It requires the user's
Mac, authorized configuration, and an actual supported Secure MCP Tunnel/plugin
connection. Do not create credentials, install software, or configure persistent
access without the required user authorization.

1. Start the configured foreground adapter via the supported tunnel flow. Use a
   new isolated route/state DB and test Codex configuration; keep the old actor
   idle. Do not claim a WebSocket or downloaded schema creates native tools.
2. Parent enumerates the actual tool inventory and calls `bridge_status`. Spawn
   one actual native child with the requested model/effort and a short task. Child
   independently enumerates and calls the same tool. Verify identical logical
   route, bounded state, actual admission identity, and available tool signatures.
   If either cannot call it, stop at this concrete capability blocker.
3. Send one small Codex request advertising its current read-only `exec_command`
   schema. Child reads the bootstrap once and loads that exact schema once. Ask it
   for three dependent stdout-only commands. First command generates a fresh nonce
   on the Mac; subsequent commands use the actual preceding returned nonce/output.
   Use the currently advertised arguments, no files/network/credentials, and no
   command execution in the bridge or cloud fixture.
4. For all three callbacks, preserve the exact call ID, exact action ID, output
   bytes, source revision, logical route, and same actual native child. Confirm
   each arrives as the result of `submit_action_and_wait_result` (or same-action
   `await_result` after timeout), with one bootstrap and only new deltas. Final
   answer must depend on the actual third Mac output.
5. Include one read-only command that lasts beyond the configured MCP application
   wait. Observe `pending`, call same-action `await_result`, and count one Mac
   invocation. Then test a duplicate action, a wrong callback ID, and cancellation
   before commitment; each must fail closed or reconcile without replay. After
   commitment cancellation explicitly says it is too late, never claims rollback.
6. Measure timestamps at native tool entry, bridge commit, HTTP emit, Mac execution
   start/end, callback HTTP ingest, native tool return, and next native output.
   Capture bytes and operation counts separately. Report p50/p95 only after enough
   real runs; one successful fixture is not an end-to-end latency promise.

Acceptance is three actual dependent Mac callbacks, one actual native child,
correct output-derived final text, and no re-execution under timeout/retry. Stop
on missing tool visibility, unknown ownership/context, changed authorization,
incorrect callback binding, or unsupported host timeout behavior.
