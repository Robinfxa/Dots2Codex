# Loopback Responses facade

This is the runnable integration layer, not a model server. `runtime.py` composes:

- `transport.Queue`: durable request/result identity, binding, reservations,
  deduplication, expiry and cancellation
- `context.ContextStore`: locally validated full source requests, exact schema
  lookup, explicit context receipts, and model-visible full/delta delivery
- `mcp_adapter`: eight static tools over real MCP stdio
- `wire.py`: request/response validation and SSE serialization copied from the
  frozen Dots2Codex baseline; no Drive controller, OAuth, model API, or Mac executor

`create_runtime(config)` creates no listener. The user-invoked adapter starts
`start_http('127.0.0.1', port)` and closes it on exit. POST `/v1/responses` (also
`/responses`) requires the configured local bearer, exact loopback Host, bounded
Content-Length, and no Origin or Transfer-Encoding. The HTTP bearer is injected
from the launching environment, never put in example JSON or logs. There is no
public HTTP bind, background auto-start, installer, or deployment step.

## Contract

All authority is trusted installation input: grant, route, session, thread, model,
reasoning effort, client/worker logical IDs, context epoch, authorization window,
and approval reference. These fields cannot be overridden in model tool arguments.
The approval reference is bookkeeping, not proof of authorization by itself.

- `get_request(after_seq, wait_ms)`: initial full or next delta. Returns stable
  request ID, context token and exact payload; repeated acquisition replays it.
- `discover_tools(request_id, context_token, query, limit)` and
  `lookup_schema(request_id, context_token, name, sha256)`: exact current tools as
  data. The schema response has a receipt token.
- `submit_action_and_wait_result(request_id, action_id, response, context_token,
  schema_tokens, wait_ms)`: validates the native Responses output, publishes it to
  the waiting Codex request, waits for the next verified callback, and returns its
  delta. Unchanged schemas need no repeat lookup; pass an empty schema token list.
- `await_result(request_id, action_id, wait_ms)`: observes an already committed
  action, without another intent emission.
- `finish_request(request_id, action_id, response, context_token, schema_tokens)`:
  commits a message-only native final response.
- `cancel_request(request_id)`: stops unpublished work when possible. Once a
  response is committed, returns `too_late_result_committed`, effect unknown.
- `bridge_status()`: bounded metadata and operation counts; never secret values.

The wrapper preserves exact native response IDs, call IDs, namespace, arguments,
and output. No model inference or tool outputs are synthesized in product code.
Fixture response constructors live only in test/fixture modules and are explicitly
marked synthetic.

## Fail-closed behavior and limits

- Same action ID with different contents is rejected. Exact submission retry does
  not create another response. A duplicate HTTP stream cannot replay a tool intent:
  the in-process emission fence is burned before write. Lost delivery is unknown.
- Queue reservation survives process death. A replacement runtime cannot acquire
  reserved work and emit it again. This release does not transparently resume
  native context or partially emitted streams after restart. Use read-only state
  inspection and explicit reconciliation; a fresh route cannot retroactively undo
  an unknown old side effect.
- Source requests still use complete history. They are checked locally, then only
  the new exact delta is exposed to the model. No output truncation or silent
  summary is used. A full projection above the inline bound fails explicitly.
- The facade supports text plus advertised function/custom tools supported by the
  pinned wire validator. Hosted tools, unsupported content forms, changed model/
  effort, unknown callbacks, altered prior calls, and missing current exact schema
  receipts fail closed. Arbitrary Codex versions remain unverified.
- Each route is a single active conversation. Native logical ownership is not
  platform attestation. Single-owner stdio/tunnel trust is required; no public
  multi-user isolation claim is made.
- One existing route DB cannot be treated as fresh after restart. This keeps
  at-most-once local admission conservative instead of claiming exactly-once Mac
  execution across crashes.

## Verification

From the prototype root:

    python3 -m unittest facade.test_runtime orchestration.test_vertical_slice -v
    python3 -m orchestration.vertical_slice

The second command uses real HTTP and real MCP stdio, with synthetic worker/client
outputs. It proves wiring and callback correlation, not actual model availability,
host tool visibility, Secure MCP Tunnel latency, or Mac execution.

See `../orchestration/README.md` for the actual native/Mac acceptance gate.

## Provenance

`wire.py` is copied from
`Dots2Codex-lite-next-frozen/dots_lite/wire.py` (baseline identified by the parent
as `af7bac6`) with its support import changed to `wire_support.py` and strict inner tool-argument
numeric projection checks added (lossy decimals and negative zero fail closed). The small
support module implements that validator's required error/canonical/hash helpers.
The project MIT license is preserved separately. No source binary or credential
from the prior bridge is included.
