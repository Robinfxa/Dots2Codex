# Bounded global native controller, protocol v2

Status: the Google queue, native helper, per-child v3 JOIN adapter and desktop Global launcher are integrated and tested with offline ports. No actual Google/native-controller/Mac acceptance has been performed. `production_ready` remains false. The normal config-first `client-config-trial/1` transaction requires a fresh sealed authenticated native preflight, exact `codex-cli 0.159.2` catalog-adapter evidence, a selected safe `CODEX_HOME`, and explicit redacted exact-diff confirmation; ordinary production apply stays closed. A control Doc and a Python process do not create or wake a native agent.

## Authority and scope

A user sends one private `DOTS2CODEX_GLOBAL_JOIN_V2` message to an already authorized conversation. It names one activation, one queue Doc/tab and one random JOIN code. The signed root additionally fixes the exact folder, six existing worker source hashes, controller-helper source hashes, expiry, quotas, and the exact versioned controller timing policy (900-second freshness; 25-second target cadence). This grants the bounded controller only the stated transport/control workflow. It is not standing permission for unrelated files, OAuth, wider sharing, arbitrary tool execution or a new future activation. The 900-second policy applies only to fresh activations; old signed 180-second roots fail closed and must never be rewritten or resumed with widened timing.

The trusted active Router must obtain its own actual native task identity from the platform. Never use a task ID asserted by a Doc as evidence of your identity. The JOIN code authenticates shared protocol data, not the platform or underlying model. Model admission receipts are parent-recorded submitted arguments and returned task IDs; `underlying_model_verified=false` remains accurate.

Each route contains only a canonical identity hash, exact model/effort, generation, expiry and a signed child bootstrap. User prompt/history/tool output does not go into the admission queue. It flows only after the separate child's immutable pin and readiness are verified, using the original per-session message/CAS protocols.

## Components

- `global_gateway.py`: fixed loopback port, route pinning, bounded local admission queue and one-attempt request forwarding
- `global_control.py`: strict signed event projection and exact `requiredRevisionId` CAS plans
- `global_native.py`: private active-controller ledger; one-use native spawn arguments, real-result recording and verified parent-to-child admission import
- `global_handoff.py`: exclusive private hash-bound child descriptor; read-only consumption after verified parent import
- `global_google.py`: authorized Mac-side Google adapter; separate v3 child bootstrap/control Docs, probes, pins, journals and facades
- `router_join.py`: retains the selected-v3 parent-recorded admission fence; a dedicated parent-only adapter imports exact verified global admission into a separate fresh child ledger
- `router_pairing.py` and `native_connector/router_pairing.js`: bounded startup in an already imported child, with genuine raw probes and original admission/runtime validators
- The existing inference/selection APIs remain; the shared tool adapter gains a validated raw-response primitive. Exact worker/controller source hashes change and require fresh activation bindings.

## Running the Mac side later, with explicit consent

These are implementation interfaces, not instructions to run them against an account without approval. The user must authorize credential reuse and this exact folder/control-resource workflow. Nothing here performs new OAuth or widens an existing grant.

1. Initialize a private durable gateway state with a fixed, available unprivileged port and reviewed default pair:

   `python3 -m remote_transport.global_gateway init --state-dir PRIVATE_STATE --port 43187 --model gpt-6.1-sol --effort high`

2. After explicit Google-control authorization, the Mac supervisor can run:

   `python3 -m remote_transport.global_google --state-dir PRIVATE_STATE --bridge-dir PRIVATE_BRIDGE --router-config EXISTING_REVIEWED_ROUTER_CONFIG --confirm-google-control`

   This standalone Google command waits up to 180 seconds on a first request while admission completes. The desktop Global supervisor uses a fixed 1800-second cold-route ceiling, bounded by the original route creation time and activation/route/controller lease expiry; no reconnect or heartbeat renews it. Every progress tick and final dispatch recheck live authority, pause and stop fences. It emits valid `response.created`/`response.in_progress` JSON events, not comment-only keepalives. The prompt remains only in bounded handler memory until dispatch. It normalizes only the outer Responses ID across admission and inference; original backend response IDs are retained privately and tool item/call IDs remain unchanged. A pre-dispatch disconnect does not later execute the abandoned prompt. After a timeout the user must submit again; after uncertain dispatch there is no automatic replay.

   This binds the fixed port before refreshing the approved credential or creating resources. It creates one control queue and writes the private JOIN to `PRIVATE_BRIDGE/global-join.txt`. It does not send the JOIN automatically and does not change Codex global settings. Its normal status does not print the JOIN code.

3. The user sends that JOIN once. The active native controller completes the steps below. A stale or finished controller produces an explicit not-ready condition; no Python fallback spawns an agent.

4. The desktop Global supervisor first runs a nonce preflight on a separate pinned native child. Only a live authenticated controller, exact completed native route, matching client/parser/package bytes, fresh signed evidence and an explicitly reviewed config transaction allow pilot apply. The production gate stays closed. Do not edit readiness booleans to bypass either gate. The active controller must continue supervising so each later desktop thread receives its own child.

## Active native controller procedure

Use direct connected Google tools for the authorized Doc and folder. Save complete returned structured resources unchanged into private local evidence files. Never use extracted paragraphs as substitutes for the editable resource/revision. The helper expects the actual structured `documentId/revisionId/tabs` object, not a surrounding display-text wrapper.

All helper calls take:

- `--snapshot CURRENT_PRIVATE_DOCUMENT_RESOURCE.json`
- `--document-id EXACT_QUEUE_DOC --tab-id EXACT_TAB`
- `--join-code-file PRIVATE_JOIN_CODE.txt`
- `--state-dir PRIVATE_CONTROLLER_LEDGER`
- `--native-task-id ACTUAL_PLATFORM_CONTROLLER_TASK`

The CLI is `python3 -m remote_transport.global_native OPERATION ...`.

1. `inspect`: verify the root, source bindings, authenticated event chain and local anti-rollback ledger. Check the authorized folder, deadline, quotas and exact pending selections. Doc payloads cannot supply an executable prompt or add protocol keys.
2. Use `emit-cell --cell-operation join --package-root VERIFIED_PACKAGE --capacity N --seconds BOUNDED_SECONDS --save NEW_CELL.js` with the common arguments. It writes a complete reviewed `functions.exec` cell containing the reviewed capture adapter, the new controller adapter and its configured invocation. Emission does not execute the cell. Choose N no greater than available platform child slots and the signed cap; the parent consumes its own slot. The generated cell runs one bounded sequence of actual connected Docs calls, including an exact readback. Keep its source private; it contains local paths and resource IDs, though it does not embed the JOIN secret. Execute these exact generated bytes in the active native agent, not Node or Python. Node is only an offline test harness.
3. The JOIN cell performs fresh read → real-clock JOIN and later first-heartbeat sampling → one durable `join-heartbeat` group plan and check → one exact CAS → full readback → verification of both ordered events. A bounded subsecond wait samples the next actual integer second; timestamps are never backdated or invented. The first JOIN timestamp still starts the original lease and 120-second acceptance budget. No status/model turn or native dispatch separates the pair. Report active only after the complete signed pair verifies; partial JOIN evidence grants no liveness or replay.
4. While genuinely active, use `emit-cell --cell-operation heartbeat ... --save NEW_CELL.js` and execute the emitted one-shot cell. Target the next heartbeat at the prior signed heartbeat time plus 25 seconds. If a cell already consumed that interval, start the next due cell without another 25-second sleep. Each tick groups fresh read, plan, check, one actual write, readback and local acceptance with no model/status gaps. Freshness is strictly `0 <= now - heartbeat_at < 900`; at 900 seconds it is dead. This 15-minute allowance is not a 15-minute heartbeat schedule. The immutable activation and controller lease still expire at their original times. Never run a Python timer/daemon, automatic wake, unbounded heartbeat loop, or rejoin an expired/closed activation. While a child runs independently, keep supervising; do not block for its whole task before the next heartbeat.

   During the short signed `admitted` → `ready` interval, the standalone heartbeat
   helper yields the queue writer turn before creating a plan or reserving an
   attempt. It returns `action=heartbeat_deferred_for_ready`, `read_only=true`,
   `heartbeat_verified=false`, the count of all still-admitted routes, the unchanged
   signed heartbeat time, an original `observe_before` deadline and a
   `recheck_after_seconds` hint of at most 25 seconds. This is not a heartbeat or
   readiness acknowledgement. Continue the already-admitted child's pairing and
   allow the Mac to publish ready; do not start another route's queue writes while
   this handoff is pending. On a child progress notification, or after the bounded
   hint, invoke one new heartbeat cell with a fresh full read. Do not spin through
   deferred cells without waiting. One ready route does not end the handoff if
   another is still admitted. A new normal heartbeat plan is possible only after
   the fresh authenticated queue has no admitted routes and all existing ledger
   fences still pass. No deferred invocation has an attempted write to retry.

   The handoff ends no later than the earliest original activation expiry,
   controller lease, heartbeat freshness limit, or pending route/child-bootstrap
   expiry. Read-only rechecks do not move any of these deadlines. At the boundary,
   stop and report the existing expired/not-ready condition; do not renew, rejoin,
   or report a deferred observation as active liveness. Transfer time also counts
   against that same window. A due trailing heartbeat inside claim preparation
   that encounters this handoff stops before any native spawn reservation.
5. Before preparing a native spawn, choose a new private `NATIVE_PLAN` path plus future exact actual-argument/result files. Pre-emit the post-spawn cell from step 8. Its `--record-snapshot-file SAME_ROOT_SNAPSHOT` must already exist and be an exact verified resource of this queue/root; it only selects the local evidence ledger. It need not be the as-yet-unknown future dispatch snapshot. Native preparation will separately return its actual `record_snapshot_file` for audit. Never use the earlier snapshot to authorize a write or import.
6. Prefer `emit-cell --cell-operation claim-prepare-native --route-id ID --plan-file NATIVE_PLAN --package-root VERIFIED_PACKAGE --save NEW_CELL.js` with the common arguments, then execute its exact bytes. The cell reads fresh authority and the host-clock chooser selects exactly `claim-begin`, or `heartbeat-claim-begin` if the target heartbeat is already due. Each is a fixed ordered same-route/native-actor group under one revision-guarded indexed batch, with all original event checks and the earliest predecessor deadline. A durable group excludes legacy/component reissue; only its complete exact signed prefix can verify. After any newly due trailing heartbeat verifies, the cell passes that same invocation's accepted readback to `plan-native --check-native-now` as its final helper. No native reservation waits behind a CAS or network call. Native-exposing cells use a long outer yield allowance to reduce avoidable wrapper gaps; if a cell nevertheless yields, collect its result immediately without unrelated work.

   `claim-begin` remains a claim/begin-only cell without a native reservation when immediate native dispatch is not desired. `pre-native --plan-file NATIVE_PLAN` starts with its own fresh read for an already verified begun route; it never reuses a previous invocation's snapshot. Every native plan still burns one attempt before exposing exact arguments. Make the next real native tool call directly outside `functions.exec`, before the original `execute_before` (at most 10 seconds from preparation), with no heartbeat, status or unrelated tool in between. A late/expired plan cannot be regenerated. A platform call actually dispatched in time may return later; retain its actual result without inventing a new dispatch time.
7. The active trusted Router must now actually call `collaboration.spawn_agent` exactly once with those returned arguments. The helper/Python does not call it. The selected `model`, `reasoning_effort`, `fork_turns="none"`, task name and trusted message must match exactly. Do not substitute a default model, an existing task, a follow-up call or the first route's worker.
8. After spawn returns, durably capture the exact submitted argument object and actual tool result into those private files. Never substitute the plan for evidence of the actual call. The cell pre-emitted before dispatch uses `emit-cell --cell-operation post-spawn --route-id ID --plan-file NATIVE_PLAN --actual-arguments ACTUAL_ARGS --native-result ACTUAL_RESULT --record-snapshot-file SAME_ROOT_SNAPSHOT --package-root VERIFIED_PACKAGE --save NEW_CELL.js` with the common arguments. After actual evidence capture, execute its exact generated bytes once. The cell records actual evidence before any network step, then obtains fresh authority, performs the strict `heartbeat-admitted` pair in one checked CAS, verifies its exact ordered prefix, and runs the existing full parent import against that accepted readback in the same helper invocation. The earlier same-root snapshot identifies the local record only; it is never reused to authorize a write or import. A newly signed heartbeat never widens the predecessor freshness/lease or acceptance deadline. Any failure stops dependent steps, preserves recorded evidence and never permits another spawn. Unknown admission or missing result keeps the slot uncertain; publish `plan-unknown` only if current authenticated authority permits it.
9. The post-spawn cell performs the following unchanged admission gates. For separately invoked operations, publish/verify `plan-admitted --route-id ID`, then run `import-child-admission --route-id ID --admission-receipt EXACT_RECORDED_RECEIPT.json --child-native-task-id ACTUAL_RETURNED_CHILD_TASK` with the common parent arguments and a fresh queue snapshot. This verifies the parent's recorded native result, exact signed admitted event and verified CAS, controller epoch, route/dispatch, selection, child bootstrap and source bindings. It imports typed parent provenance and the exact receipt into the deterministic private child ledger reserved by `plan-native`: `PRIVATE_CONTROLLER_LEDGER/QUEUE_ROOT_HASH.children/ROUTE_ID`. There is no caller-selected destination, receipt-only child import, copied parent ledger or global JOIN secret in the child. The destination must still be fresh; a child must wait before running any `router_join` command or probe.
10. Only after successful import, send the exact receipt path and returned `child_state_dir` to the same actual child. Its compact message fixes an immutable private handoff path/hash outside the fresh child ledger and contains no global JOIN secret. The preferred child entry is the message's `router_pairing emit-cell --phase pair` command with its actual platform identity and exact parent receipt. Emission and every bounded phase validate the existing read-only handoff-consumption/import checks; a separate `global_handoff consume` call is optional manual inspection, not a prerequisite round trip. Execute the exact emitted child cell once. It reserves the reverse probe, overlaps independent forward/reverse raw evidence retrieval, admits with exact CAS/readback, and can continue materialize/ready from that same readback if a verified bundle already exists. Otherwise it returns an explicit waiting state; later `--phase ready` uses fresh bootstrap/control resources. No timer, automatic wake, polling loop, replacement worker or native inference is hidden in these helpers. Unknown effects cannot be retried by switching back to the manual workflow. All ledger/runtime paths, signed root, source hashes, model/effort and admission receipt remain fixed.

Import burns the parent's destination reservation before the child record is written. A repeated import may only read back the exact already-written receipt and provenance; it never repairs missing/conflicting child evidence or chooses another path. A crash before the child record is durable remains `reserved_outcome_unknown` and fail-closed. This is same-filesystem, same-owner trusted-parent evidence, not cryptographic platform attestation or protection against arbitrary local ledger rewrites. `underlying_model_verified=false` remains unchanged.
11. The Mac adapter independently verifies the child's actual v3 admission, reverse raw probe, bundle and signed `WORKER_POLLING`, consumes the verified child bundle, then verifies the global ready event before attaching only that thread's facade. A controller heartbeat alone never marks a route ready.
12. Keep supervising only for the authorized bounded lifetime. On explicit close, stop admitting, interrupt the actual owned native children using the supported platform tool, and report actual stop outcomes. Unknown stop does not free a slot. At expiry, return an accurate final state; do not claim continued background monitoring.

Do not copy the native spawn-plan arguments or child JOIN into normal status logs. They contain private handoff data. The admission receipt itself has no JOIN secret and can be passed to the specified internal child.

## Concurrency and recovery

The authoritative queue is one signed Doc per activation. Every queue mutation requires the exact read revision and one atomic pinned-tab indexed delete/insert batch; signed event history gives read-only reconciliation. The native and Mac local ledgers reject rollback and repeated attempt issuance. A known conflict and an unknown outcome are different; the current production Docs port conservatively treats ambiguous failures as unknown. It must not automatically resubmit a CAS merely because a subsequent read lacks the desired event.

The current connected `batch_update_document` wrapper exposes generic tool
results and errors, not an authoritative request-bound non-commit receipt or a
transport no-replay attestation. A revision-conflict message, structured HTTP
400/409, or an error class name is therefore insufficient to retire an attempted
reservation. In particular, a transport can lose a successful response, internally
repeat the same POST, and then report stale revision. The offline SDK regression
demonstrates this ambiguity. No error classifier in the native cell grants fresh
planning after such an attempt. Exact signed readback within the original
acceptance deadline remains the only reconciliation path.

The ready handoff reduces the observed heartbeat-versus-ready race; it does not
guarantee that all queue conflicts disappear. A different Mac demand/close write,
another native mutation, a stale/in-flight snapshot, or another writer can still
win between read and CAS. Such attempts remain unknown/no-replay. The guard runs
inside `NativeLedger.plan_event` under its normal ledger lock and verified source,
not as a separate JavaScript inspection. It does not reset an operation, introduce
new operation IDs for rejected attempts, or change the signed protocol authority.

The generated JOIN/heartbeat cell saves private categorical failure evidence before attempting its one read-only reconciliation. It records the operation, failing stage, whether a write was attempted, a fixed error category, bounded numeric HTTP/RPC status, and explicitly allowlisted provider/local error codes. It does **not** retain provider messages, display content, request bodies, stacks, URLs or arbitrary strings: errors can echo credentials or queue contents, and regex redaction is insufficient. A subsequent readback or verification failure gets a separate linked diagnostic rather than replacing the original write category. If diagnostic capture itself fails, stop without retrying the capture or any connector write.

A queue authenticated as closed before JOIN dispatch produces `global_queue_closed_before_join` and no write. Any error after dispatch remains `global_cas_outcome_unknown_no_replay` until the normal exact-event verifier succeeds within its original acceptance window. In particular, a revision-error category, a missing JOIN event, or a later closed queue is not proof that the attempted write never happened. The private native ledger separately records whether the readback authenticated, whether that queue was closed, and whether the exact expected event prefix was observed. These diagnostic facts do not accept the CAS, refresh controller liveness, clear an attempt, or allow a heartbeat/spawn. A closed readback can contain the JOIN event while the controller is still unusable. Neither case authorizes replaying the old JOIN; preserve the old evidence and use a fresh authorized activation when recovery requires it.

The gateway's SQLite is an additional local dispatch fence, not a substitute for distributed Docs CAS. Native plans are burned locally before tool arguments are exposed. Lost output after that boundary intentionally sacrifices availability rather than risking a duplicate child or side effect.

Ready-route transport restart reopens the same controller journal and acquires the original facade lease. It verifies the same signed admission/pin, request history and current queue controller before rebinding an ephemeral child-facade endpoint behind the unchanged stable gateway port. No new native spawn is made. A historically verified bootstrap may be read after its handshake window only for this existing-route recovery, with its signed terminal state, current control and unexpired deployment pin; that is not fresh worker-health telemetry. Unknown gateway request intents remain non-replayable.

One Google control session is bound to one activation generation. New threads may select any reviewed pair within that activation, but a changed default-generation URL requires a new bounded JOIN/control session. The Google adapter filters its own generation and refuses new-generation admission on the old controller. The offline gateway's multiple-generation capability is not proof of seamless live multi-generation Google orchestration.

`stop` first saves a durable local stop intent so a later old heartbeat cannot re-enable the session. It immediately fences local controller readiness, disables new admissions, attempts the signed queue close and each local facade's authoritative session close, then closes local sockets. Its result explicitly does not attest that native children stopped. Completed/late-result evidence and immutable catalogs are retained. Restoring user config remains a separate reviewed transaction and never kills clients silently.

## Indexed Global queue updates

All five Global queue planners use one shared builder: single events, `join-heartbeat`,
`claim-begin`, `heartbeat-claim-begin`, and `heartbeat-admitted`. Native controller
writes and Mac demand/ready/close writes therefore use the same two-request packet.
One `batchUpdate` contains, in this order:

1. `deleteContentRange` on the exact pinned tab with range
   `[1, 1 + UTF16(source_text_without_its_final_newline))`
2. `insertText` at index 1 on that same tab, containing the new canonical signed
   block without its final newline

Both requests share the unchanged `requiredRevisionId` from the same complete
source read. The last mandatory document newline survives the deletion and ends
the inserted block. No trimming, text normalization, search matching, second write,
or fallback replacement is involved. Google documents
[UTF-16 indices and the last-newline restriction](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request)
and [atomic batch application with one reply per request](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate).

Before making a Global snapshot, the parser verifies a complete dedicated sole
root tab, using either the raw `tabProperties`/`documentTab` shape or the connector's
verified normalized shape, including its null top-level body and absent optional
metadata represented as null. A content-bearing top-level body beside tabs is
rejected. An initial section break, when present, must cover
`[0,1)`; its zero `startIndex` may be omitted. Paragraphs and their text runs must
have contiguous exact UTF-16 integer spans beginning at 1, each paragraph must end
in one newline, and the final end must equal `1 + UTF16(full_text)`. Boolean, float,
string, absent, overlapping, gapped or inconsistent offsets are rejected. Extra
or nested tabs, mixed legacy/tab bodies, suggested edits, non-text union members,
tables, extra section breaks, headers/footers/footnotes and inline/positioned objects
are not editable Global queues. Packet reconstruction compares canonical JSON bytes
so `true` and `1.0` cannot impersonate an integer index.

For a returned response, only exactly `replies=[{},{}]` is accepted for these two
requests, together with the existing document and changed-revision checks. Empty
per-request replies are expected; a missing response is a distinct lost-response
case. Neither reply shape nor a changed revision proves success. Acceptance still
requires a fresh complete indexed readback, the exact authenticated root, a
canonical signed state, a new revision, and the full planned signed-event prefix
within the original acceptance deadline. A later valid signed event may follow the
prefix, so the response and readback revisions need not match each other. Malformed
response evidence remains a rejection; it is not silently treated as a lost reply.
Unknown outcomes, durable reservations, no replay and native-dispatch fences are
unchanged.

This change addresses the observed post-ready heartbeat content no-op. Its saved
24,660-character ASCII needle exactly matched the original document minus its
mandatory final newline, across three contiguous paragraphs. The revision changed
but the authenticated state did not, and the empty replacement response did not
prove a replacement. That evidence does not establish a provider or connector
root cause. The old failed event remains unaccepted and non-replayable. The repair
does not change child bootstrap/control or legacy cleanup, or the blank-document
queue initialization's one-insert protocol. Changed controller source hashes
require one coherent reviewed package and a fresh authorized activation; no old
signed queue is migrated or retried in place.

## Startup optimization boundaries

The fixed group whitelist is `join-heartbeat`, `claim-begin`, `heartbeat-claim-begin`, and `heartbeat-admitted`; it does not permit arbitrary event batching. Each exact signed event remains present and all predecessor deadlines survive. Controller cells reuse accepted full readbacks only within the same invocation. The child pairing cell may also reuse its initial authenticated WAITING_FOR_WORKER snapshot after probes in that same invocation; current guards still run and exact revision conflicts cannot authorize replay. Small bounded evidence envelopes preserve the exact source/response privately and run the existing plan/check or verify/import in one helper RPC; oversized envelopes retain the original chunked capture path. Explicit wide result transfer is bounded by serialized bytes, keeps exact digest/offset checks, and falls back only to reading the same immutable result file, never rerunning a planner. One-use private argument permits recheck the original deadline at actual dispatch and burn on failure, including observed clock rollback. Actual native dispatch and returned evidence remain separate real platform operations.

The known standalone and desktop client factory paths enable bounded resource preparation overlap: one Drive worker serializes Drive calls while Docs operations remain on their owner thread. Separate durable reservations precede resource creation, returned identifiers are merged serially, and failure drains in-flight work without replay. Desktop preparation additionally has a scoped owner/worker pause gate. Pause intent fences queued Drive effects, drains and preserves the result of an already-issued effect, and lets only the Docs owner reconcile a fresh authenticated queue before releasing queued work. Stop, expiry, failed reconciliation or cancellation terminate the bounded preparation without replay. Unknown injected ports retain the serial default. When a bridge already has active facades sharing its client objects, resource preparation also falls back to serial operation; the new cross-port overlap is scoped to cold preparation before those consumers exist. This is independent I/O overlap, not a generic thread-safe SDK claim.

Mac preparation reuses the same-call verified blank Bootstrap resource for exact-revision initialization and keeps the actual initialization readback. Bundle publication reuses its same-call authenticated child snapshot; a concurrent writer burns the conflict rather than triggering a retry. For first readiness, verified consumption precedes the ready CAS, so that exact global ready readback is also the later queue checkpoint. Existing-route recovery retains its own fresh gate.

The remaining dependency floor includes actual native admission evidence, exact CAS acceptance readbacks, both genuine raw probe paths, and materialized runtime plus fresh IDLE control before polling proof. Removing the fresh global ready source after consumption is an availability tradeoff: normal controller heartbeats can cause avoidable conflicts. The Global queue now uses the indexed batch described above; child bootstrap/control and legacy cleanup retain their existing literal replacement protocols. Independent safe Docs clients remain a separate design question. Live connector/platform timing is still needed before claiming an end-to-end limit.

Changed source bindings require a fresh activation. Offline operation-count improvements do not establish live Google latency, native scheduling latency, or a cold-start guarantee under three minutes. Measure verified child readiness and first meaningful response content separately from heartbeat and keepalive events.

## Timing, fences and fresh migration

Protocol v2 adds `controller_timing` to the authenticated immutable root; queue markers, MAC domains, CAS packets, native ledgers, Google mirror mode and control protocol are versioned. The reviewed controller source set includes timing, gateway, pilot, the full cell driver, and the child `router_join.py`/`router_bootstrap.py` admission boundary. Both endpoints must use this same reviewed package. V1 activations, old local controller state and old source hashes fail closed; there is no in-place migration or permission to replay the old JOIN. After explicitly stopping/closing the prior activation, retain all evidence, create a new activation with fresh state/Docs/code under the existing authorized workflow, and send that new V2 JOIN. Do not alter the old signed root, delete attempt journals, or widen the old lease.

Event timestamps are prepared using the trusted helper host wall clock, not Google `modifiedTime` or a revision ID. A prepared CAS has an exact derived acceptance deadline: the minimum of preparation + 120 seconds, activation expiry, and (for controller events) the previous accepted heartbeat + 900 seconds and controller lease expiry. Both dispatch check and exact readback acceptance must finish before it. Signing a newer timestamp does not prove a provider committed it on time. Native and Mac ledgers therefore retain the previously trusted heartbeat, event chain and clock observation; a stale or backward-clock observation durably fences that root/epoch before a newer snapshot can be adopted. Gateway checks have a separate durable fence that survives a rejected SQLite transaction. Re-reading a snapshot or status never refreshes heartbeat freshness. If any endpoint did not observe its controller within the bound, recovery requires a fresh activation, even if it later sees a newer-looking tick.

Unknown CAS outcomes permit only exact read-only reconciliation within the same acceptance window. An absent event is inconclusive. No new heartbeat, claim or other native CAS may be planned while an earlier operation remains unresolved, and unknown child admission continues consuming its slot. A failed/lost/expired operation is not authorization to reissue it with another operation ID.

New desktop activations reserve the preflight route before issuing a nonce challenge or HTTP request. Desktop prewarm, cold-route admission wait and child bootstrap now have an explicit maximum of 1800 seconds, always clamped by the original signed activation/controller/route deadlines. This is a safety ceiling, not an expected wait. The standalone Google command retains admission wait 180 seconds and default child bootstrap 600 seconds; generic Gateway defaults to no wait. Other deadlines remain unchanged: desktop preflight UI wait 180 seconds, initial supervisor-stage wait 90 seconds, POST/nonce plan 600 seconds, sealed proof 300 seconds, and request execution 180 seconds. New activations use 900-second controller freshness with the same real 25-second target heartbeats, not a renewable setup lease. Existing signed roots and bootstrap expiries are never widened; old 180-second timing and changed source bindings require a fresh activation. A failed or closed supervisor now requires a new activation instead of suggesting the old one can resume. `native_children_stopped=false` means stop was not attested, not that a child is known to exist.

## Live acceptance still required

- Actual connected Google resource creation, exact CAS, raw-file access in both directions and credential scope
- Actual active native controller admission, selected per-thread child tools and finite platform capacity
- Tool timeouts/latency versus heartbeat budget, Mac sleep/reboot and controller exit
- Exact terminal catalog adapter and safe selected CODEX_HOME; full consumer restart including applicable app-server/managed daemon, then a fresh post-commit new-thread route. Signed-app and second-binary checks apply only to explicitly selected advanced profiles
- Full real function/custom-tool loops and uncertain delivery; old/resumed threads/provider retention
- Auth/config preservation, all nine still-skipped real tomlkit integration tests (including newer comments beside owned scalars or inside the provider table), project/managed/profile/CLI override scope
- Signed/unsigned Mac launcher behavior and explicit global-config confirmation

No normal-flow app installation/name/bundle-ID/signature/internal-binary check is required. The scope is local consumers reading the selected config, not all ChatGPT Work or cloud tasks. A completed route proves traffic through the gateway, not app-process identity or underlying-model attestation.

## Offline diagnostic regressions

Run `node --test native_connector/global_controller_cell.test.js` and
`python3 -m unittest remote_tests.test_global_native_diagnostics -v` from this checkout.
The fixtures cover a lost/error-shaped write followed by a failed or closed readback,
closed-before-JOIN without a write, observed-but-expired events, failed private capture,
and credential/document-content sentinels. All connector and clock inputs are synthetic;
these results are not live Google, Mac or native-admission acceptance.

Ready/heartbeat handoff regressions: `python3 -m unittest
remote_tests.test_global_heartbeat_ready_handoff -v` and `node --test
native_connector/global_controller_heartbeat_handoff.test.js`. They cover genuine
signed readiness, multiple admitted routes, no-reservation deferral, fresh revision
selection after ready, expiry/closure/clock fences, and definitive synthetic
rejection versus untrusted/stringly/ambiguous connector errors. None permits replay
of an attempted write or infers non-commit from an absent expected event.
