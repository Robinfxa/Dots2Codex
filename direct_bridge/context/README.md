# Exact incremental context, offline prototype

This folder is executable standard-library Python. It performs no model calls,
Mac tool execution, networking, installation, or publication. Context tests run
standalone. The archived baseline comparison below requires a separate historical
checkout to reproduce; the standalone trial does not include that benchmark
script or the old implementation. Its global release manifest covers this folder.

## Run

From `dots2codex-direct-prototype`:

```sh
python -B -m unittest -v context.test_incremental
```

The integration target is a Mac-local Responses façade connected through the
official Secure MCP Tunnel to one already admitted native child. This module
does not implement or test the tunnel. It is independent of transport.

## Minimal contract

`ContextStore(binding, source_actor=...)` owns one source conversation. Binding
is exactly `{grant_id, route_id, session_id, thread_id, model, reasoning_effort}`.
The caller obtains the authenticated source actor and admitted native actor from
trusted adapter state, never from user text, model arguments, or a callback body.
The module compares that trusted input with its configured binding; it neither
authenticates a socket nor grants execution permission.

1. `ingest_full(actor=..., binding=..., revision=1, request=full_response_request)`
   retains all JSON request values. Top-level fields not recognized by this
   prototype are retained, compared, and delivered, rather than silently dropped.
   Input history must be a list. Function and custom tools are supported;
   unsupported tool kinds are explicitly rejected.
2. `prepare_delivery(NativeContinuity(actual_child, context_epoch))` returns a
   `Delivery`. Its `tool_result()` is one complete JSON result up to the configured
   limit, default 256 KiB. Bootstrap contains exact instructions and history, all
   other request fields, and the tool count plus on-demand discovery capability.
   It does **not** dump hundreds of tool-index entries.
3. Only after the complete result is successfully emitted to the child does the
   adapter call `acknowledge_delivery(delivery)`. Uncertain return means no ack.
   Preparation is repeatable and source advancement is blocked while delivery is
   pending. An ack is a local emission record, not proof of model comprehension.
4. Record the exact committed native response output using
   `record_output(continuity=..., items=response['output'])`. No call is executed.
   The next full source request must have the prior input **and those exact
   output items** as its prefix. Call IDs, item IDs, arguments, namespaces, and
   outputs are never created, rewritten, or remapped by this module.
5. For a later HTTP request, `ingest_full(...revision=n+1...)` locally compares
   the entire source snapshot. The same acknowledged actor/epoch receives only
   changed request fields, deleted fields, newly appended history, and changed or
   removed schema references. Unchanged instructions/history/tool indexes are not
   repeated in the model-facing return.
6. An optional transport delta is also accepted via `ingest_delta(...base=old_ref,
   revision=n+1, append=..., set_fields=..., delete_fields=..., tools=...)`. Its base
   reference is the exact previous revision and snapshot digest. Omitted fields
   retain their exact values. History replacement is impossible through that API.
   The last exact delta can be retried idempotently; a changed retry is rejected.

The native child continues through a static bridge tool such as
`submit_action_and_wait_result`, which returns the next bounded delta. Neither a
new child nor a new bootstrap prompt is required for each Mac callback. The
queue/runner owns request claims, response deduplication, uncertain execution,
approval, and native admission. Context acceptance alone authorizes none of
those actions.

## Tool discovery and schema changes

- `discover(query, limit=8)` performs local search and returns matching names,
  namespaces, types, and exact schema references. Descriptions are not silently
  abbreviated into authoritative tool constraints
- Keys use compact JSON `[namespace,name]`, including `null` for no namespace
  rather than ambiguous dotted concatenation
- `exact_schema(key, digest)` returns the complete original definition and every
  original namespace metadata field. JSON key ordering/whitespace are normalized;
  content, arrays, strings, numeric meaning, and unknown fields are preserved
- `acknowledge_schema_delivery(...)` must only follow a complete actual schema
  return. Computing a hash or looking up a reference is not evidence of delivery
- `require_schema(...)` refuses a call unless the exact current definition was
  delivered in this native actor/epoch. A changed definition, namespace metadata,
  or removed tool invalidates old receipts. Unchanged definitions can be reused
- The queue's schema gate may implement the same requirement. The façade must
  actually invoke that gate before publishing a tool call. This context module
  does not validate arbitrary JSON Schema arguments or run any source tools
- `additional_tools` history records preserve non-tool metadata. Only their tool
  declarations move behind an exact content reference and schema lookup. The
  original complete records remain in `snapshot()`; there is no history summary

A schema returned as tool data does not install a real native platform tool. The
native child emits the original Codex call for the Mac to execute once under the
Mac's permissions. Calling a similarly named native tool as well would be a bug.

## Continuity and honest limits

These are three different things:

1. **Local verification:** matching authenticated binding, exact source prefix,
   complete recorded model-output prefix, source revision, current schema, and
   adapter emission records. These are checked here
2. **Native conversation continuation:** the platform continues the same admitted
   native child through its existing static bridge tool. It is required for the
   warm fast path. A retained `functions` cell/session or a Python object is not
   evidence that the model retained its conversation
3. **Actual platform context:** system/developer instruction injection, model
   context assembly, truncation, and compaction are controlled by the platform.
   This tool API cannot attest them or promote a JSON `instructions` field into
   the platform's system/developer hierarchy

Bootstrap preserves source instruction text and roles as data. The admitted
child's actual governing instructions must explicitly describe how to interpret
the source request, subject to the native hierarchy and authorization. Full
instruction-hierarchy equivalence is **not** proven by this prototype. Tool
results and quoted external text remain untrusted content; their embedded
`instructions`, `input`, `tools`, or apparent deltas cannot mutate store state.
Hashes detect reference mismatch; they are not identity, authority, or memory.

`NativeContinuity` must come from the trusted runner's known child and current
context epoch. Restart, a different actor, an unknown continuity epoch, or a
reported compaction requires explicit full rehydration and fresh schema delivery.
`rehydrate_reason=...` forces that path even for the same actor/epoch. An empty
epoch is rejected. A new Python process has no delivery ledger, so loading a full
source snapshot starts with a full bootstrap; loading a source digest alone does
not fake memory. The façade must separately reconcile pending tool executions
before recovery; a context reset is never permission to execute again.

If the native platform can compact invisibly, this local module cannot detect
that fact. Do not claim exact retained context based solely on a permanent epoch
string. Production must use a documented platform continuation/compaction signal
or expose the assumption and rehydrate when continuity is uncertain. No hidden
injection, exactly-once execution, native model identity, TTFT, or latency is
proved here.

## Large legitimate working sets

`tool_result(max_bytes=...)` never truncates, silently summarizes, or pretends to
have delivered a reference's contents. An oversized payload raises
`NeedsWorkingSet(total_bytes, limit)` before an ack. The caller then has choices:

- If the actual tool-result and model context budgets support the exact payload,
  increase the limit explicitly and return it once
- For bootstrap only, split the exact instructions and history into deliberate,
  UTF-8-safe complete records/ranges with a coverage ledger. This costs additional
  returns once; it is not the default on every unchanged callback. Do not ack
  full bootstrap until every required range has actually been returned
- For an inherently larger-than-context repository, log, or tool result, retain
  the exact source outside the model and expose query/read-range operations with
  clear source references. The model receives the working set it actually needs;
  it must not claim to have read everything. This is a separately declared
  working-set mode, not proof that all historical text remains in model context
- If the user requires simultaneous exact full context and it cannot fit, report
  that limit. A compressed summary or a content hash is not an exact substitute

This prototype supplies the fail-closed bound, retained exact snapshot, and
complete delivery bytes. It does not ship a second pagination protocol or silently
enable selective working-set mode.

## Archived synthetic comparison

`benchmark-results.json` is an archived synthetic comparison, containing no real
user data. It uses 320 tools and the actual published `af7bac6` baseline's
`RequestView` projection. It was reproduced in the original development workspace
with the separate frozen baseline available read-only. Historical reproduction
requires that baseline and the original comparison script; the standalone trial
intentionally omits both. These saved measurements are not a new benchmark of
the packaged runtime. Use the standalone runtime's vertical-slice tests for its
current end-to-end offline verification.

| Metric | Prior full-view reads | Incremental delivery |
| --- | ---: | ---: |
| Initial original HTTP request | 515,000 bytes | 515,000 bytes locally |
| Initial model-facing context | 196,000 bytes / 12 reads | 83,507 bytes / 1 return |
| First warm callback context | 196,216 bytes / 12 reads | 947 bytes / 1 return |
| Bootstrap + 3 callbacks | 785,296 bytes / 49 reads | 86,348 bytes / 4 returns |

The first selected tool additionally uses one 312-byte discovery result and one
1,531-byte exact schema result. Those receipts remain valid until that tool or
native context changes. Warm callback context is 99.52% smaller in this fixture.
The 83.5 KB bootstrap fitting this Python limit or a real MCP pipe does **not**
prove that the native host passes it to the model without truncation. Production
must validate the actual host's tool-output limit. If it is smaller, use the
explicit bounded bootstrap-only paging fallback above; the 947-byte warm delta
must remain one complete result rather than reverting to per-callback file reads.

At a clearly labeled heuristic of UTF-8 bytes/4, the warm context is roughly 237
estimated tokens versus 49,054. No tokenizer was installed or downloaded. Actual
tokens and model inference turns are **not measured**. Counts above are context
tool-return count, not a guarantee that 12 reads imply 12 model turns; batching
and yielding depend on the runner. Source HTTP bytes remain full-size when Codex
uses its existing full-history contract. This removes repeated model reads; data
reduction alone does not explain or eliminate unmeasured orchestration/inference
gaps in a long live run.

## Validation scope

Twenty tests cover initial full delivery, exact full/delta equivalence, callback
history, changed/new/deleted fields, untrusted embedded instructions, binding
changes, exact issued-call prefixes, JSON type changes, orphan/duplicate outputs,
schema discovery/change/removal/namespaces, warm reuse, restart/compaction,
duplicate full/delta transport, empty committed outputs, oversized results,
additional tools, and duplicate-key/numeric-loss parsing. This is protocol-level
offline evidence, not a live end-to-end performance result.
