# Remote-object prototype runbook

This is the original single-writer mode. The opt-in [Docs-CAS mode](DOCS_CAS_RUNBOOK.md)
adds authoritative conditional transitions and known-file-ID discovery. Its durable
journal enrollment prevents accidental downgrade to the plain commands below.

## Status and preserved baseline

The remote transport is a separate package alongside the preserved local POSIX
queue and routing implementation. The root README now describes both paths; the
previous local installation guide is retained in `LOCAL_BRIDGE_README.zh-CN.md`.
Use [remote setup](docs/REMOTE_SETUP.zh-CN.md) for downloadable client examples and
endpoint configuration, and [remote validation](docs/REMOTE_VALIDATION.md) for the
observed connector/native text roundtrip and its limits.

This runbook describes the optional plain single-writer mode, not a
live-certified ready-to-install remote Codex deployment. Offline tests do not
access Drive or configure OAuth. Real setup and cloud operations require their
own explicit authorization.

## What the prototype proves

- A pinned session/controller/worker/generation/journal identity, never remote
  discovery of “the newest” generation
- Hash-linked deployment → request → claim → dispatch-intent → result → receipt
- Durable one-use invocation-permit issuance under the designated single-owner,
  non-cloned, non-rolled-back local journal assumption
- No native retry after intent, timeout, uncertain effect, or missing result
- Explicit publication recovery of fixed bytes, including physical Drive duplicates
- Delayed/reordered objects wait for complete predecessor chains; observed history
  never rolls backward because a subsequent list omits an item
- Partial/incomplete pagination, unreadable blobs, collisions, changed content and
  inconsistent journal indexes fail closed
- Separate delivery evidence: result existence does not prove client/user receipt

A same filename is not a grab/lock because Drive permits multiple files with the
same name. SHA names deduplicate content; “list, see none, then upload” has a race.
A preallocated shared Drive file ID gives a one-shot unique creation primitive,
not a renewable lease or fencing of a previously running native worker. It requires
API capabilities not exposed by the present connected plugin.

## Local offline validation

Python 3.11+ on POSIX with working flock, fsync, atomic rename, private directories.
Do not use a synced folder or shared network filesystem for either role's journal.
The remote core tests use Python 3.12 and need only the standard library. The full
repository tests require requirements-test.txt; optional Google client examples
need requirements-google-example.txt.

```sh
cd Dots2Codex
python3 -m unittest discover -s remote_tests -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q remote_transport remote_tests
python3 -m remote_transport.demo --root /tmp/dots-drive-demo-UNIQUE
```

The demo explicitly uses LocalFSBackend and synthetic answers, with zero native
calls and zero Drive calls. Its object directory is an interface-conformance store,
not the legacy `BRIDGE_ROOT`. Do not call this a live remote test.

## API surface and ownership

`deployment(session_id, native_task_id)` creates a NEW deployment with fresh journal
incarnations. `Journal.provision(path, pin, role)` creates a NEW local role journal.
Only do this once when no prior execution can exist for that session. `Journal(path,
pin, role)` is resume: missing or malformed state halts. Use the exact trusted pin
on both hosts and exactly one controller host and one worker host. The paths do not
need to be shared between hosts. Do not copy a live journal or restore an old backup
and run it alongside or instead of the current owner.

Controller: `publish_deployment`, `submit(text, idempotency_key)`, `result(request_id)`,
`record_delivery(request_id, result_id, evidence)`. Repeating a submit key with the
same bytes returns its ID; it does not silently re-upload or execute. Another key
cannot advance until the prior delivery receipt is durably recorded and published.

Worker: `start_next` returns either pending (`None`) or a single invocation permit.
The active authorized native worker may consume that fresh permit ONCE. Invoke the
actual supported native/tool path outside this package and call `complete(permit,
text)` only with the actual result. Python does not implement that native boundary.
Never cache a permit as a resumable work item. A restarted worker with a dispatch
intent must reconcile its actual native execution; it must not invoke it again.

`reconcile()` reads all bounded pages, validates bytes and merges a monotonic local
view. `poll(check, ...)` makes at most eight checks with capped delays; pending or
transient read errors remain unresolved, not proof that execution never started.
`recover_publication(object_id)` explicitly republishes the exact saved object and,
in strict mode, the same saved physical ID. It never yields an execution permit.
The role cannot recover someone else's upload. An HTTP wait deadline or disconnect does not cancel the original pending remote
request: its designated worker can still dispatch it until deployment expiry. A
504 means unresolved delivery, not permission to resubmit a new native attempt.
No API changes worker generation or
performs failover. Human review of an unresolved native outcome is required.

Receipts and `complete` may be saved after the deployment expires to preserve known
outcomes; expiration prevents fresh requests and permits. It never causes replay.
Evidence strings are attestations from the caller, not independently verified proof.

## Drive API capability gates

`GoogleDriveBackend(client, folder_id, mode='strict_ids')` requires an injected API
port with a verified complete paginated raw-blob listing implementation and
create-by-preallocated-ID support. Before upload it persists the exact bytes and
allocated ID. A lost response is resolved by exact readback or explicit same-ID
retry; 409 does not mean success until byte comparison succeeds.

`mode='duplicate_tolerant'` drops only the create-by-ID requirement. It still requires
complete raw-blob enumeration. An explicitly retried ambiguous upload may create
another physical file. Byte-identical logical objects collapse, conflicting semantic
slots halt, and native dispatch still depends on the one durable worker journal.

The present connected Drive plugin does NOT provide generateIds/create-by-ID and
has NOT established complete paginated enumeration of raw application/json objects.
Consequently it is NOT marked conformant to the general mailbox adapter. A small
live connector smoke can instead pass every exact returned file ID between roles;
that verifies bytes and fixed-ID handoff only, not general discovery or dispatch.

`remote_transport.drive_http.DriveHTTPClient(token_provider)` is a separately
implemented raw REST port with pagination and preallocated IDs. It makes no network
calls at import and reads no credential sources. The caller must supply an already
separately authorized token provider; tokens remain in request memory and are not
logged/persisted by this module. The adapter's request construction has offline
coverage; it has not yet been exercised against the real Drive service.

The connected dot plugin's OAuth grant is not accessible to this Python process and
must not be extracted from browser storage, caches, or native tools. Each actual
endpoint needs a user-authorized Drive client/setup of its own. Any new persistent
OAuth access requires the user's action-time approval. Do not introduce a model
relay per message to paper over this missing authorization/deployment boundary.

## Runnable facade and worker commands

`python3 -m remote_transport.cli --help` lists the entrypoints. Transport selection
is explicit: `--transport localfs --object-root PATH` for offline validation, or
`--transport drive --folder-id ID --client-factory reviewed_module:create_client`.
The latter factory must return an independently authorized API client. There is no
automatic fallback, credential-file reading or model relay. `--drive-mode` selects
strict IDs (default) or explicitly duplicate-tolerant publication.

Run commands in the repository root. A bounded offline manual session can use:

```sh
work=$(mktemp -d)
python3 -m remote_transport.cli new-deployment --pin "$work/pin.json" --session demo --native-task-id synthetic/native
python3 -m remote_transport.cli init-local-store --object-root "$work/objects"
python3 -m remote_transport.cli provision-journal --pin "$work/pin.json" --journal "$work/controller" --role controller
python3 -m remote_transport.cli provision-journal --pin "$work/pin.json" --journal "$work/worker" --role worker
python3 -m remote_transport.cli publish-deployment --pin "$work/pin.json" --journal "$work/controller" --transport localfs --object-root "$work/objects"
python3 -m remote_transport.cli serve --pin "$work/pin.json" --journal "$work/controller" --transport localfs --object-root "$work/objects" --ready "$work/ready.json"
```

The final command stays active until deployment expiry or Ctrl-C. In another
terminal/native worker context, using that same demo work path:

```sh
python3 -m remote_transport.cli worker-next --pin "$work/pin.json" --journal "$work/worker" --transport localfs --object-root "$work/objects" --save "$work/permit-1.json" --wait 20
# Only the admitted native worker may consume the fresh packet once.
# After it has the actual result, save a private JSON file {"text":"actual answer"}:
python3 -m remote_transport.cli worker-complete --pin "$work/pin.json" --journal "$work/worker" --transport localfs --object-root "$work/objects" --permit "$work/permit-1.json" --result "$work/result-1.json"
```

The facade serves `POST /v1/responses`, `model=native-subagent-bridge`, `stream=true`,
canonical `session-id` and UUID `thread-id` headers. Host must be its loopback
address; Origin and Authorization headers are rejected. It accepts bounded text
message history, preserves tool advertisements as context, and returns text only.
Tool-call input/output is rejected. The exact full wire request is hashed and
carried to the worker in `request.responses_request`, with `scope=text_only`.
The emitted stream contains the frozen output-item/completed SSE events.

The facade pins the first canonical client identity durably. Socket flush is only
`socket_flushed`, never a delivery receipt. A next turn can acknowledge the prior
result only by including the exact emitted assistant item (including its ID and
content) in its full input history. If the actual CLI omits or transforms that item,
the next turn safely stops until explicit delivery confirmation. This behavior
has synthetic HTTP coverage but is not yet verified against the actual CLI.

For the final result, stop the facade, then use `ack-delivery --request-id ID
--evidence "Observed the result in the client"` with the same pin, journal and
transport arguments. Only do that after actually observing it. This command
refuses to race another running facade. Repeating a confirmed acknowledgment
returns the prior receipt. An in-process caller can use `confirm_delivery`.

The facade journals client state separately from transport state. A crash between
those commits can leave an index mismatch and deliberately halt restart. Do not
delete/recreate its state; inspect existing requests/results and resolve manually.
This conservative limitation prevents silently reassigning a client or replaying
a possibly executed request. A valid old backup remains unsafe: no file protocol
can prove it was not rolled back while remote visibility is delayed.

The subprocess test `remote_tests/test_cli.py` runs separate facade, worker-next,
and worker-complete processes for two synthetic SSE turns, then verifies the final
receipt. This is stronger than an in-memory store test but still LocalFS transport
and synthetic results, not Drive/native inference/independent-machine evidence.

## Separately approved live validation gates

1. Obtain permission for one dedicated private synthetic test folder and bounded
   raw JSON uploads/readbacks. Use synthetic text only. No existing personal files,
   sharing changes or deletes are needed. The test should retain its evidence.
2. Connector smoke: upload one deployment/request pair, pass exact returned IDs to
   a separate reader role, fetch bytes and verify OIDs/pins, upload synthetic
   claim/started/result/receipt objects, fetch and verify. Label every result
   “synthetic transport smoke; no actual native model invoked”. Never infer a daemon
   can use the connector simply because both roles can call its native tool.
3. Raw REST gate: after separate credential setup, verify complete pages, generated
   ID retry/409/readback, folder metadata and strict publication. Do not claim any
   of this from a mock or the connector smoke. Probe only the dedicated folder.
4. Independent-machine gate: connect the existing facade through an explicitly
   reviewed remote queue/relay, run controller and pinned worker on separate hosts,
   admit a genuine native worker through the supported task tool, and observe two
   real CLI turns and their delivery receipts. Preserve local journals independently.
   The separate remote facade/worker CLI is implemented and offline-tested. The
   existing launcher's readiness handshake/profile staging has not been adapted or
   live-validated for it; do not point the old launcher at this service.
5. Only after all gates, consider deployment packaging. There is no automatic
   background wake: file arrival does not wake dot or allocate a native worker.
   An active worker/operator or separately authorized scheduler must perform polling.

## Recovery table

| Condition | Safe action |
|---|---|
| List/media timeout, 403/429, partial pages | Preserve state, fix access or back off; retry read |
| Upload outcome unknown, no visible match | Keep exact intent; optionally explicitly recover same blob |
| Dispatch intent exists, no actual outcome | Stop execution; inspect supported native task outcome |
| Result saved but upload lost | Publish fixed result bytes; never infer again |
| Result read but delivery uncertain | Resolve actual client delivery; never infer again |
| Conflict, corrupt payload/index, wrong pin | Stop session and investigate; do not erase blocker |
| Missing/rolled-back worker journal | No recovery by empty journal or list absence; manual review |
| Old worker times out | No automatic replacement, generation bump, lease takeover or replay |

Hashes/actor fields provide integrity, not hostile-writer authentication. Every
writer with access to the folder is within the trust boundary. Production multiwriter
coordination needs a proper authoritative transaction service, plus fencing honored
at the actual execution target. Drive metadata alone cannot supply that guarantee.
