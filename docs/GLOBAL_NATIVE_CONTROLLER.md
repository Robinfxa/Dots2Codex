# Bounded global native controller, protocol v1

Status: the Google queue, native helper and per-child v3 JOIN adapter are implemented and tested with offline ports. No actual Google/native-controller/Mac acceptance has been performed. `production_ready` and global-config apply remain false. A control Doc and a Python process do not create or wake a native agent.

## Authority and scope

A user sends one private `DOTS2CODEX_GLOBAL_JOIN_V1` message to an already authorized conversation. It names one activation, one queue Doc/tab and one random JOIN code. The signed root additionally fixes the exact folder, six existing worker source hashes, controller-helper source hashes, expiry and quotas. This grants the bounded controller only the stated transport/control workflow. It is not standing permission for unrelated files, OAuth, wider sharing, arbitrary tool execution or a new future activation.

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

4. Only after separate production acceptance and an explicitly reviewed config transaction may the launcher offer global apply. Do not edit readiness booleans to bypass this gate.

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
2. `plan-join --capacity N --seconds BOUNDED_SECONDS --save NEW_PLAN.json`: reserve one join CAS attempt. Choose N no greater than currently available platform child slots and the signed cap. The parent/controller consumes its own platform slot; configured capacity does not reserve native platform slots.
3. Execute the returned direct Docs `tool_arguments` once. Save the exact response and a fresh full readback. `verify --plan-file PLAN --response RESPONSE --readback READBACK` must succeed before continuing. Unknown response: omit `--response` and read back the exact signed event. Absence is inconclusive and never authorizes another write.
4. While the native controller is genuinely active, fetch the queue on a bounded cadence. Publish `plan-heartbeat` roughly every 10 seconds. Freshness is currently 30 seconds; a slow/hung tool call can expire the controller. That is fail-closed and needs explicit recovery/new JOIN, not a fake heartbeat from the Mac. This timing is an unverified live acceptance constraint.
5. For each selected pending route, use `plan-claim --route-id ID`, execute/verify its CAS, then `plan-begin --route-id ID`, execute/verify its CAS. Saturation and unknown children keep their slots; there is no stealing/reusing a child from another thread.
6. `plan-native --route-id ID --package-root VERIFIED_PACKAGE --save NEW_NATIVE_PLAN.json`. This checks current controller/child-bootstrap lifetimes, closed state, exact source hashes and ownership. It durably burns the native attempt before returning arguments.
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

## Live acceptance still required

- Actual connected Google resource creation, exact CAS, raw-file access in both directions and credential scope
- Actual active native controller admission, selected per-thread child tools and finite platform capacity
- Tool timeouts/latency versus heartbeat budget, Mac sleep/reboot and controller exit
- Installed terminal CLI and desktop bundled CLI separately, full restart/new-thread route receipts
- Full real function/custom-tool loops and uncertain delivery; old/resumed threads/provider retention
- Auth/config preservation, tomlkit format-preserving integration (including newer comments beside owned scalars or inside the provider table), managed/profile/CLI override scope
- Signed/unsigned Mac launcher behavior and explicit global-config confirmation
