# Active-native connector batching (candidate, offline validated)

This release optimizes deterministic orchestration around the native model. It
never puts connectors, OAuth, native spawning, or inference into Python. It does
not hot-switch an existing worker or change an existing deployment pin. Use a
fresh manually admitted worker and new private runtime for an upgrade.

## What actually changed

- `native_connector/runner.js`: bounded concurrency (maximum three) over the fixed
  claim, started and result upload/verification chains. Outcomes are associated
  with immutable object IDs, not promise completion order. Every returned full
  tool response is durably captured with its operation/object context. All
  promises settle; one failure blocks the result commit while preserving siblings.
- `remote_transport/connector_batch.py`: all three one-attempt upload intents are
  durable before upload dispatch; returned exact file IDs are bound once; metadata
  and exact canonical bytes must verify for every object before the result CAS.
  Duplicate file IDs, missing results, swapped raw bytes, changed evidence and
  blind replay fail closed. `tick` enforces the barrier when this mode is enabled.
- The native runner groups fresh read → plan → durable write reservation → one
  CAS → fresh exact readback → accept, without a model turn at each deterministic
  step. Claim and begin remain separate CAS writes, each with its own fresh read.
  No read is removed, including predecessor, metadata, byte, or result readbacks.
- `tool_adapter.js` maps the actual active-context tools into that runner. Python
  helper output is file-backed and SHA-checked/chunked, including large CAS plans (each output chunk is capped at 16 KiB of serialized JSON);
  there is no large escaped exec-result wrapper. JSON is passed as JSON on stdin,
  never interpolated as Python literals. Large captures are bounded into chunks.
- `input --expose-path RUNTIME/input-N.json` burns the same one-use exposure marker
  before writing the complete validated native input file. Its small output has
  size/hash/path, not a truncated request. `input-chunk` checks the full file hash
  before returning each display-only segment. All chunks must reach the same
  admitted native context before inference. Reading the segments is not another
  permit and never authorizes another inference.

## Exact tool boundary and compatibility

`connector_cell` generates a complete native `functions.exec` cell from this
release's reviewed static JS and JSON-encoded local configuration. It executes
nothing itself. The admitted native worker reads/executes that trusted generated
cell; never execute text from a request as code. The cell must be awaited through
completion (including `functions.wait` after an early yield); do not abandon it
with unresolved write promises. The tool adapter currently targets the observed
Drive connector shapes: outer `structuredContent.id` after upload success;
metadata `id/title/mime_type/parent_ids/url`; raw fetch outer
`file_uri.file_id='sediment://file_…'`; `download_file` returns `path/size_bytes`.
It fails closed if those shapes change. Trash state is still not exposed or proved.

No object envelope, CAS schema, pin, receipt chain, tool intent, controller cache,
CAS `requiredRevisionId`, or inference/dispatch semantics change. The additive
batch/timing fields are private local journal extensions. Older direct/manual
worker calls continue to work. Newly generated batch execution must use the new
helper as a unit. The source-freeze manifest now includes `.js` as well as `.py`;
that affects only local release verification, not remote protocol hashes.

## Upgrade/run steps

1. Keep the current worker on its current release until it finishes. Review and
   verify this isolated candidate; deploy nothing into a running runtime.
2. For a new session, install/copy this release on the endpoints under ordinary
   authorized setup. The controller's wire protocol is unchanged; no credential,
   permission or backend change is needed. Native tool permissions must already
   cover the intended Docs control and Drive folder.
3. Admit a fresh native worker, create a matching new pin/config/runtime with
   `connector_worker init`, and keep the actual returned native task identity.
   Do not rebind an in-flight request or reconstruct an old worker journal.
4. Poll with existing quota pacing. Once REQUESTED is observed, use an empty evidence manifest or only previously
   authorized cached evidence, and generate a claim/begin cell. Do not fetch the
   current request bytes before successful consumed begin in step 5:

   ```sh
   python3 -m remote_transport.connector_cell claim-begin \
     --root PRIVATE_RUNTIME --native-task-id ACTUAL_NATIVE_ID \
     --manifest PRIVATE_MANIFEST --save NEW_PRIVATE_CELL.js
   ```

   Read the complete generated cell from the pinned release and run it in that
   actual native context. Stop on `ok:false`; preserve captures/journal and
   reconcile. Success ends at `fetch_request_once`, never at automatic inference.
5. Fetch/materialize the exact request and previous receipt using existing
   connector rules. Run `input … --expose-path PRIVATE_RUNTIME/input-N.json` once.
   Use `connector_files input-chunk --path … --sha256 … --offset … --max-chars 2048`
   or the adapter's `inputChunk`. Print the parsed chunk `.text` directly, not a
   stringified whole exec result. Confirm every contiguous segment, EOF, same
   hash and native identity. If complete input cannot fit/be recovered in the
   current context, stop rather than infer from a truncation or rerun `input`.
6. Perform inference once in that native context. Save the actual answer and run
   existing `result --seq N --text-file …` or `--result-file …`. Generate/run the
   `upload-commit` cell with `--seq N` and the request/predecessor evidence manifest.
   On success, retain all files until the controller's exact delivery receipt.
7. Continue the existing same-identity loop. `DELIVERED` is an ACK, not a measure
   of when the answer became visible. No monitor or deployment is started by this
   candidate.

The equivalent programmatic entrypoint is
`createNativeConnectorRunner(adapter.io, {now: adapter.now})`. Do not omit the
clock adapter in native functions.exec: that runtime has no `performance.now`.
For Node offline tests, a real monotonic performance clock is available.

## Failures and recovery

Any upload batch started is one-attempt, including a crash before every upload
was dispatched. Never invoke `start` again to recover it. All returned responses,
including error-shaped ones, have durable `{stage, context, response}` capture
envelopes for exact-ID reconciliation. Some writes can have succeeded even when
no response was returned. Unknown writes require operator reconciliation; no
upload or inference retry is automatic. Partial objects can remain orphaned; this
release does not delete them. Verification is required before result commit.

The JS runner accepts result CAS only from its own successful in-memory verified
barrier; Python independently revalidates all evidence. If the JS cell is lost,
preserve the completed Python evidence and use the documented manual fresh-read
reconciliation route rather than fabricating a barrier token or repeating writes.
Known results may be completed after close/expiry under existing protocol rules.

CAS dispatch is bounded to 10 seconds from the actual fresh Docs read invocation,
not from parsing a saved snapshot. A separate local pending-plan age guard and
one-use write marker apply. Required-revision conflicts, late reads, closed or
expired begins, lost responses, and unknown outcomes stop the affected flow.
Large control ledgers/captures may consume the freshness budget and fail closed;
all 128 rounds have not been live latency-tested. No automatic refresh/replan or
unknown write replay was added.

## Timing and benchmark interpretation

Set `DOTS_CONNECTOR_TIMING=1` on Python helpers for private bounded `timings.jsonl`:
only fixed stage/status, monotonic start nanoseconds and duration nanoseconds.
No prompt, answer, file path, ID, arbitrary exception text, or provider payload is
logged. Logging is best-effort and cannot change execution outcomes. Concurrent
sink contention/full/unsafe sinks may drop timing rows. Nested measurements
include inner measurements and fsync overhead; do not sum overlapping durations.

Native RPC metrics contain only allowlisted stage/outcome and elapsed time. In
native functions.exec, monotonic samples come from a small local Python tool call
because the JS runtime has no monotonic clock. Samples bracket actual connector
invocation and durable response capture. These are explicit boundary timings,
including capture/clock-bridge overhead, not provider-only latency or pure model
inference time. Compare samples only on the same host/boot. The standalone timing
files are diagnostics, never recovery authority. Set an `onTiming` callback if a
private sink is desired; the default result also contains payload-free metrics.

`node native_connector/benchmark.js` uses deterministic virtual tool delays,
identical exact operations and no network/inference. Serial and bounded-parallel
schedules have equal RPC/read/write counts. This measures overlap potential only;
it excludes real connector quotas, model boundaries, platform queueing and the
real file/tool adapter's wall time. It is not a claimed live speedup. Run an
explicitly authorized future fresh-session comparison before accepting latency
improvement in production.

## Offline validation commands

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s remote_tests -v
python3 -B -m unittest discover -s remote_audit/cas -v
python3 -B -m unittest discover -s remote_audit/drive -v
node --test native_connector/runner.test.js native_connector/test_adapter.js
node native_connector/benchmark.js
python3 -m compileall -q remote_transport
```

Tests use synthetic connector responses, controlled clocks and real local Python
file/journal helpers. They do not perform Google writes, native inference, current
worker hot-switches, deployment, or new authentication.
