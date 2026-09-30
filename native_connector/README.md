# Native connector orchestration

`runner.js` is trusted, released orchestration code, not a transport client. It
contains no imports, shell commands, credentials, connector authentication,
network client, native admission, inference, daemon, wake, or retry loop. Copy its
source into the already-admitted native worker's `functions.exec`, together with
the reviewed `tool_adapter.js`, and inject that context's own authorized tools.
Do not execute request text or dynamically obtained JavaScript. Node is only an
offline test host here.

The Python helpers remain the authoritative protocol/journal validators. Native
connector calls remain in the actual admitted context. A success from this runner
is not proof of account access, provider performance, an active executor, or
successful native inference.

## Public factory and methods

`createNativeConnectorRunner(io, options)` returns:

- `uploadVerifiedBatch({seq})`: start one durable batch, execute its three immutable
  upload/evidence chains, await every outcome, and run the Python final barrier
- `commitVerifiedBatch(batchResult)`: consume that same runner's successful barrier
  result once, obtain a new actual control read, and attempt the result CAS once
- `uploadAndCommit({seq})`: the two preceding operations together
- `freshCasCycle({expectedKind, evidence})`: one fresh `claim` or `begin` cycle;
  direct `result` calls are rejected
- `claimAndBegin({evidence})`: claim/readback/accept, then a **different fresh**
  read/begin/readback/accept. It stops at `fetch_request_once`

`options.concurrency` defaults to 3 and accepts integers 1–3. This bounds complete
independent object chains, not merely simultaneous upload calls. Metadata must
complete before raw fetch because the provider-returned URL is needed. No names,
folder scans, "latest" selection, invented download URLs, or position-based
association are used.

`options.maxDispatchAgeMs` defaults to 10000 and may only reduce that cap. The age
starts at the monotonic sample immediately preceding the actual control read. It
includes read, capture, planning, reserve, and scheduling time. The check after
the durable reserve occurs on the final sample before invoking the native CAS
callback. Parsing old JSON never creates fresh evidence.

`options.now` is a trusted monotonic milliseconds callback. Both a number and a
Promise of a number are supported. Samples are serialized across concurrent
chains, and nonfinite/regressing values fail closed. `performance.now()` is only
a default where that API actually exists. The native `functions.exec` runtime
may lack it; pass the adapter's `now` callback there. There is **no `Date.now()`
freshness fallback**. The actual adapter's local Python monotonic-clock bridge
adds local tool round trips; it is not connector authentication or a substitute
for native connector calls.

`options.onTiming` optionally receives frozen records with exactly
`{stage, outcome, elapsed_ms}`. Stages are fixed public constants; outcomes are
`returned` or `threw`. No prompt, request/operation/object/file ID, path, URL,
provider message, stack, or arbitrary error text is included. Returned responses
are durably captured before the completion sample, so these are **local RPC
boundary timings including capture and clock-bridge overhead**, not isolated
provider or wire latency. Async clock bridge time is not model latency. Timing
callback errors are ignored; correctness never depends on telemetry.

## Strict injected IO contract

All callbacks below are async-compatible and use one object argument. The IO
implementation is trusted released code; this interface cannot make an adapter
that fabricates/caches reads or neglects `fsync` safe. Keep callbacks scoped to the
fixed private runtime, admitted task identity, authorized folder, and control
record. All local output handles below are nonempty private strings.

1. `startBatch({seq})` calls the Python batch `start` exactly once. It must validate
   the immutable local claim/started/result descriptors and **durably burn all
   three write intents before returning**, with atomic/exclusive replay checks.
   Return `{action:'upload_batch_once',batch_id,objects}`. Each of exactly three
   objects has `{object_id,path,name,mime_type:'application/json',folder_id}`.
   Hashes and names are unique and all objects use one approved folder.
2. `upload({object})` makes exactly one direct native upload call with that fixed
   path/name/MIME/folder, returning the complete tool response. Do not retry.
3. `capture({stage,context,response})` persists the entire returned tool response
   and its context association durably before returning a private response-file
   handle. For uploads, context contains `batch_id` and `object_id`; CAS responses
   contain `operation_id`. Save the association even for error-shaped or malformed
   responses. Do not log these envelopes or expose them as timing records.
4. `recordUpload({batch_id,object_id,response})` asks Python to parse the saved
   complete upload response. Require actual `structuredContent.success === true`
   and its exact returned `id`; persist the object-to-file-ID association and
   reject duplicate IDs across the batch. Return `{reference}`, where reference
   is `{object_id,locator:{backend:'drive',folder_id,file_id}}`.
5. `getMetadata({reference})` reads metadata for that exact returned file ID and
   returns the complete response. The runner captures it before validating ID,
   title, MIME, and containing parent. Normalized metadata has no verified trash
   state; no claim about it is made.
6. `fetchRaw({reference,metadata,metadata_capture})` uses only the captured exact
   metadata response's verified provider URL and the connector's supported raw
   download route. The full returned response is captured before materialization.
7. `materializeRaw({reference,response})` resolves the actual returned raw-file
   reference into private raw bytes. If it uses another tool such as
   `download_file`, it must save that tool's **full** response and association
   itself before later operations. Return a private raw-byte file handle. Never
   use a text wrapper, re-serialized JSON, base64 guessed from a URL, or another
   object's file.
8. `verifyUpload({batch_id,object_id,metadata,raw_file})` invokes the offline Python
   exact metadata, canonical raw bytes, hash, and immutable saved-file verifier.
   Return `{verified:true}` only after durable verified evidence is saved.
9. `finalize({batch_id,items})` invokes the Python all-verified barrier. It must
   validate all three exact references again and merge existing request evidence
   without duplicates. Persist the merged manifest, then return
   `{verified:true,manifest:<private handle>}`. The runner does not infer this
   success from three callback completions alone.
10. `readControl({purpose})` performs a **new actual** full native Docs read every
    time, requesting `documentId,revisionId,suggestionsViewMode,tabs`. Purpose is
    `plan` or `readback`. Never return a previously saved response here.
11. `planCas({snapshot,evidence})` runs offline Python `tick` with that just-captured
    full snapshot and evidence. Return the complete `cas_write_once` packet with
    the exact `kind`, `operation_id`, and `tool_arguments`. Arguments retain
    `write_control.requiredRevisionId`, never `targetRevisionId`.
12. `reserveWrite({operation_id})` atomically persists an exclusive one-attempt
    marker for that exact pending Python plan before any CAS dispatch. Return
    `{reserved:true}`. Unknown, rejected, or lost responses never authorize retry.
13. `casWrite({tool_arguments})` passes the exact immutable packet arguments to
    one direct native Docs update. No rewriting revisions or requests and no
    retry. Its complete response is captured; the next protocol operation is a
    new complete native readback.
14. `acceptCas({plan,response,readback})` invokes Python `accept` with the saved
    full response/readback. Return `read_control` for successful claim/result or
    `fetch_request_once` for successful begin. Python validates revision linkage,
    operation membership, task/binding/expiry, and one-use begin consumption.

All batches use `Promise.allSettled`, so one failure cannot discard other exact
successful IDs or evidence. Outcomes stay in original descriptor order and each
includes its object hash, even when it failed. Any failed chain prevents final
barrier and result CAS. A barrier token is in-memory identity-bound and one-use:
a JSON clone or a different runner cannot use it. After a crash, reconcile exact
saved evidence through the normal Python worker flow; do not re-upload or replay
CAS to recreate this token.

A durable upload intent or CAS marker may be burned even if no external call
ultimately happens (e.g. a capture, clock, quota, or age failure). That is a safe
blocker, not permission to retry. Preserve all private files and report the exact
pending operation to the parent for supported reconciliation. The runner exposes
only fixed error codes; full sensitive diagnostic responses remain in captures.

## Native input boundary

A successful begin is not input exposure and is not inference. Fetch and verify
the exact request/predecessor receipt, then consume the existing Python `input`
permit in the **same actually admitted native task**. The adapter's file-backed
input helpers preserve the native task ID, fixed packet hash, contiguous bounded
chunks, and one-use exposure marker. Only the actual native model context may
consume that permit and perform the requested inference. The runner deliberately
contains no inference callback, context creation, request replay, or shell/model
substitute.

## Offline tests and synthetic benchmark

Run from this isolated checkout:

```
node --test native_connector/runner.test.js
node native_connector/benchmark.js
```

Tests cover reordered completion, full capture, partial successes, lost upload and
CAS responses, verifier/capture failures, duplicate returned IDs, false barriers,
forged/replayed barrier tokens, replay across new runners, exact revisions,
separate claim/begin reads, monotonic age after reservation, regressing and async
clocks, a native-like realm without `performance`, and telemetry privacy.

The deterministic virtual-latency benchmark compares concurrency 1 against 3
using the same fixed delays and protocol operations. Current fixture: 866 ms vs
444 ms, a 1.95× synthetic scheduling ratio. Both make 12 native connector RPCs,
2 required fresh control reads, and 4 writes. It preserves required safety calls
and overlaps only independent immutable evidence chains. These figures omit
real native tool/shell/clock-bridge overhead, connector variability, and model
turn overhead; **they are not measured real-world latency or a production speedup
claim**. The benchmark has no live writes, network, auth, deployment, or push.
