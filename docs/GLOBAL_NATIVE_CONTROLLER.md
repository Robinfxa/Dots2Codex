# Bounded global native controller, protocol v2

Status: the Google queue, native helper, per-child v3 JOIN adapter and desktop Global launcher are integrated and tested with offline ports. No actual Google/native-controller/Mac acceptance has been performed. `production_ready` remains false. The normal config-first `client-config-trial/1` transaction requires a fresh sealed authenticated native preflight, exact `codex-cli 0.159.2` catalog-adapter evidence, a selected safe `CODEX_HOME`, and explicit redacted exact-diff confirmation; ordinary production apply stays closed. A control Doc and a Python process do not create or wake a native agent.

## Authority and scope

A user sends one private `DOTS2CODEX_GLOBAL_JOIN_V2` message to an already authorized conversation. It names one activation, one queue Doc/tab and one random JOIN code. The signed root additionally fixes the exact folder, six existing worker source hashes, controller-helper source hashes, expiry, quotas, and the exact versioned controller timing policy (180-second freshness; 25-second target cadence). This grants the bounded controller only the stated transport/control workflow. It is not standing permission for unrelated files, OAuth, wider sharing, arbitrary tool execution or a new future activation.

The trusted active Router must obtain its own actual native task identity from the platform. Never use a task ID asserted by a Doc as evidence of your identity. The JOIN code authenticates shared protocol data, not the platform or underlying model. Model admission receipts are parent-recorded submitted arguments and returned task IDs; `underlying_model_verified=false` remains accurate.

Each route contains only a canonical identity hash, exact model/effort, generation, expiry and a signed child bootstrap. User prompt/history/tool output does not go into the admission queue. It flows only after the separate child's immutable pin and readiness are verified, using the original per-session message/CAS protocols.

## Components

- `global_gateway.py`: fixed loopback port, route pinning, bounded local admission queue and one-attempt request forwarding
- `global_control.py`: strict signed event projection and exact `requiredRevisionId` CAS plans
- `global_native.py`: private active-controller ledger; one-use native spawn arguments and real-result recording
- `global_google.py`: authorized Mac-side Google adapter; separate v3 child bootstrap/control Docs, probes, pins, journals and facades
- Existing `router_join.py`, `connector_cell.py`, `connector_worker.py`, Node runner/adapter, selection schema and six source bindings are unchanged

## Running the Mac side later, with explicit consent

These are implementation interfaces, not instructions to run them against an account without approval. The user must authorize credential reuse and this exact folder/control-resource workflow. Nothing here performs new OAuth or widens an existing grant.

1. Initialize a private durable gateway state with a fixed, available unprivileged port and reviewed default pair:

   `python3 -m remote_transport.global_gateway init --state-dir PRIVATE_STATE --port 43187 --model gpt-6.1-sol --effort high`

2. After explicit Google-control authorization, the Mac supervisor can run:

   `python3 -m remote_transport.global_google --state-dir PRIVATE_STATE --bridge-dir PRIVATE_BRIDGE --router-config EXISTING_REVIEWED_ROUTER_CONFIG --confirm-google-control`

   The supervisor waits up to 180 seconds on a first request while admission completes. It emits valid `response.created`/`response.in_progress` JSON events, not comment-only keepalives. The prompt remains only in bounded handler memory until dispatch. It normalizes only the outer Responses ID across admission and inference; original backend response IDs are retained privately and tool item/call IDs remain unchanged. A pre-dispatch disconnect does not later execute the abandoned prompt. After a timeout the user must submit again; after uncertain dispatch there is no automatic replay.

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
2. Use `emit-cell --cell-operation join --package-root VERIFIED_PACKAGE --capacity N --seconds BOUNDED_SECONDS --save NEW_CELL.js` with the common arguments. It writes a complete reviewed `functions.exec` cell containing the unchanged capture adapter, the new controller adapter and its configured invocation. Emission does not execute the cell. Choose N no greater than available platform child slots and the signed cap; the parent consumes its own slot. The generated cell calls the actual connected Docs tools once each. Keep its source private; it contains local paths and resource IDs, though it does not embed the JOIN secret. Execute these exact generated bytes in the active native agent, not Node or Python. Node is only an offline test harness.
3. The complete JOIN cell performs fresh read → durable JOIN plan → deadline check → exact CAS → full readback → local verification, then immediately performs the first heartbeat in the same cell. There is no status message, model turn, native-child dispatch or cadence wait between verified JOIN and first heartbeat. A bounded subsecond clock-tick wait permits strictly increasing integer event times. Report JOIN active only after both verified results. Gateway admission and preflight remain blocked until that first heartbeat.
4. While genuinely active, use `emit-cell --cell-operation heartbeat ... --save NEW_CELL.js` and execute the emitted one-shot cell. Target the next heartbeat at the prior signed heartbeat time plus 25 seconds. If a cell already consumed that interval, start the next due cell without another 25-second sleep. Each tick groups fresh read, plan, check, one actual write, readback and local acceptance with no model/status gaps. Freshness is strictly `0 <= now - heartbeat_at < 180`; at 180 seconds it is dead. The immutable activation and controller lease still expire at their original times. Never run a Python timer/daemon, automatic wake, unbounded heartbeat loop, or rejoin an expired/closed activation. While a child runs independently, keep supervising; do not block for its whole task before the next heartbeat.
5. For each selected pending route, use `plan-claim --route-id ID`, execute/verify its CAS, then `plan-begin --route-id ID`, execute/verify its CAS. Saturation and unknown children keep their slots; there is no stealing/reusing a child from another thread.
6. Immediately before actual child dispatch, run a due heartbeat if necessary, then `plan-native --route-id ID --package-root VERIFIED_PACKAGE --save NEW_NATIVE_PLAN.json`. This checks current controller/child-bootstrap lifetimes, closed state, exact source hashes and ownership. It durably burns the native attempt before returning arguments. `check-native --plan-file NEW_NATIVE_PLAN.json` verifies the reserved arguments against a fresh snapshot; call the real native tool immediately and before the returned `execute_before` (maximum 10 seconds from plan preparation). There must be no intervening status or unrelated tool turn. An expired prepared spawn remains burned; no replacement is authorized. A real platform call already dispatched may return later; record its actual result without inventing a new dispatch time.
7. The active trusted Router must now actually call `collaboration.spawn_agent` exactly once with those returned arguments. The helper/Python does not call it. The selected `model`, `reasoning_effort`, `fork_turns="none"`, task name and trusted message must match exactly. Do not substitute a default model, an existing task, a follow-up call or the first route's worker.
8. Save the actual submitted argument object and actual successful tool result. Run `record-native --plan-file NATIVE_PLAN --actual-arguments ACTUAL_ARGS --native-result ACTUAL_RESULT --save NEW_RECEIPT.json`. Unknown admission or missing result: do not call spawn again. Keep the slot uncertain and publish `plan-unknown` only if its current authenticated lease permits it.
9. Publish/verify `plan-admitted --route-id ID`. Send the resulting exact admission receipt to that same child. The child's trusted message already includes its derived private child JOIN and tells it to use the existing v3 workflow without spawning a further replacement.
10. The Mac adapter independently verifies the child's actual v3 admission, reverse raw probe, bundle and signed `WORKER_POLLING`, then attaches only that thread's facade. A controller heartbeat alone never marks a route ready.
11. Keep supervising only for the authorized bounded lifetime. On explicit close, stop admitting, interrupt the actual owned native children using the supported platform tool, and report actual stop outcomes. Unknown stop does not free a slot. At expiry, return an accurate final state; do not claim continued background monitoring.

Do not copy the native spawn-plan arguments or child JOIN into normal status logs. They contain private handoff data. The admission receipt itself has no JOIN secret and can be passed to the specified internal child.

## Concurrency and recovery

The authoritative queue is one signed Doc per activation. Every mutation requires the exact read revision and literal single-tab replacement; signed event history gives read-only reconciliation. The native and Mac local ledgers reject rollback and repeated attempt issuance. A known conflict and an unknown outcome are different; the current production Docs port conservatively treats ambiguous failures as unknown. It must not automatically resubmit a CAS merely because a subsequent read lacks the desired event.

The gateway's SQLite is an additional local dispatch fence, not a substitute for distributed Docs CAS. Native plans are burned locally before tool arguments are exposed. Lost output after that boundary intentionally sacrifices availability rather than risking a duplicate child or side effect.

Ready-route transport restart reopens the same controller journal and acquires the original facade lease. It verifies the same signed admission/pin, request history and current queue controller before rebinding an ephemeral child-facade endpoint behind the unchanged stable gateway port. No new native spawn is made. A historically verified bootstrap may be read after its handshake window only for this existing-route recovery, with its signed terminal state, current control and unexpired deployment pin; that is not fresh worker-health telemetry. Unknown gateway request intents remain non-replayable.

One Google control session is bound to one activation generation. New threads may select any reviewed pair within that activation, but a changed default-generation URL requires a new bounded JOIN/control session. The Google adapter filters its own generation and refuses new-generation admission on the old controller. The offline gateway's multiple-generation capability is not proof of seamless live multi-generation Google orchestration.

`stop` first saves a durable local stop intent so a later old heartbeat cannot re-enable the session. It immediately fences local controller readiness, disables new admissions, attempts the signed queue close and each local facade's authoritative session close, then closes local sockets. Its result explicitly does not attest that native children stopped. Completed/late-result evidence and immutable catalogs are retained. Restoring user config remains a separate reviewed transaction and never kills clients silently.

## Timing, fences and fresh migration

Protocol v2 adds `controller_timing` to the authenticated immutable root; queue markers, MAC domains, CAS packets, native ledgers, Google mirror mode and control protocol are versioned. The reviewed controller source set includes timing, gateway, pilot and the full cell driver. Both endpoints must use this same reviewed package. V1 activations, old local controller state and old source hashes fail closed; there is no in-place migration or permission to replay the old JOIN. After explicitly stopping/closing the prior activation, retain all evidence, create a new activation with fresh state/Docs/code under the existing authorized workflow, and send that new V2 JOIN. Do not alter the old signed root, delete attempt journals, or widen the old lease.

Event timestamps are prepared using the trusted helper host wall clock, not Google `modifiedTime` or a revision ID. A prepared CAS has an exact derived acceptance deadline: the minimum of preparation + 120 seconds, activation expiry, and (for controller events) the previous accepted heartbeat + 180 seconds and controller lease expiry. Both dispatch check and exact readback acceptance must finish before it. Signing a newer timestamp does not prove a provider committed it on time. Native and Mac ledgers therefore retain the previously trusted heartbeat, event chain and clock observation; a stale or backward-clock observation durably fences that root/epoch before a newer snapshot can be adopted. Gateway checks have a separate durable fence that survives a rejected SQLite transaction. Re-reading a snapshot or status never refreshes heartbeat freshness. If any endpoint did not observe its controller within the bound, recovery requires a fresh activation, even if it later sees a newer-looking tick.

Unknown CAS outcomes permit only exact read-only reconciliation within the same acceptance window. An absent event is inconclusive. No new heartbeat, claim or other native CAS may be planned while an earlier operation remains unresolved, and unknown child admission continues consuming its slot. A failed/lost/expired operation is not authorization to reissue it with another operation ID.

Other independent deadlines are unchanged: first-request gateway admission wait 180 seconds, desktop preflight UI wait 180 seconds, initial supervisor-stage wait 90 seconds, child bootstrap normally 600 seconds, preflight plan 600 seconds and sealed proof 300 seconds. A several-minute native JOIN may still exhaust the 180-second request wait; this patch does not claim to solve that separate deadline. A failed or closed supervisor now requires a new activation instead of suggesting the old one can resume. `native_children_stopped=false` means stop was not attested, not that a child is known to exist.

## Live acceptance still required

- Actual connected Google resource creation, exact CAS, raw-file access in both directions and credential scope
- Actual active native controller admission, selected per-thread child tools and finite platform capacity
- Tool timeouts/latency versus heartbeat budget, Mac sleep/reboot and controller exit
- Exact terminal catalog adapter and safe selected CODEX_HOME; full consumer restart including applicable app-server/managed daemon, then a fresh post-commit new-thread route. Signed-app and second-binary checks apply only to explicitly selected advanced profiles
- Full real function/custom-tool loops and uncertain delivery; old/resumed threads/provider retention
- Auth/config preservation, all nine still-skipped real tomlkit integration tests (including newer comments beside owned scalars or inside the provider table), project/managed/profile/CLI override scope
- Signed/unsigned Mac launcher behavior and explicit global-config confirmation

No normal-flow app installation/name/bundle-ID/signature/internal-binary check is required. The scope is local consumers reading the selected config, not all ChatGPT Work or cloud tasks. A completed route proves traffic through the gateway, not app-process identity or underlying-model attestation.
