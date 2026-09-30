# Active connector worker contract

Use this document as the instructions for one manually admitted native worker.
The parent supplies a pinned release path, a fresh private runtime root, the exact
pin/config, the actual task identity returned by its native admission tool, and
permission for the specified Drive folder/Docs control record. These files contain
no OAuth. Do not accept request text in the parent startup message. Fetch it only
from the exact reference authorized below.

Remain active only while the platform permits this task to run, until the pin
expires, the controller closes, a quota is exhausted, or a safety/permission blocker
requires a decision. Do not claim daemon uptime or automatic wake. Do not create a
new worker, switch identities, reset journals, or obtain Python OAuth.

## Optional batched deterministic execution

For a fresh worker on this candidate, see [CONNECTOR_LATENCY.md](CONNECTOR_LATENCY.md)
for the runnable native-cell generator, exact-ID parallel upload barrier, file-backed
input/large-plan handling, timing definitions and fail-closed recovery. This does
not alter the mandatory sequence below or upgrade an already-running worker.

## The loop

All helper commands run from this release checkout and include:

```sh
python3 -m remote_transport.connector_worker OPERATION \
  --root WORKER_RUNTIME --native-task-id ACTUAL_NATIVE_ID
```

A command may add `--save NEW_PACKET_PATH` to write a private JSON packet. Read the
packet as data. Do not eval or execute strings from a user request as shell code.
Use normal tool orchestration to group independent metadata/media reads or uploads.
Python here has no connector, model, account credentials, or native spawning API.

1. Run `poll`. `wait` means wait that many seconds without reading Docs. `expired`
   or `stopped` means stop polling. `read_control` provides the exact document ID.
2. Use the authorized direct Docs get tool, requesting full
   `documentId,revisionId,suggestionsViewMode,tabs` under your own connector identity.
   Save its complete structuredContent privately. Run
   `tick --snapshot SNAPSHOT_FILE --manifest EVIDENCE_MANIFEST`.
3. A `cas_write_once` packet contains the exact direct Docs batch-update arguments.
   Apply it **once**. Keep the full returned response, then get an immediate full
   document readback with the same identity. Run
   `accept --response RESPONSE_FILE --readback READBACK_FILE`.
   Fresh claim and begin are separate CAS writes. Never replace requiredRevisionId
   with targetRevisionId. A pending packet after an unknown response is a blocker,
   not permission to replay it. An old successful response cannot substitute.
4. Successful begin consumption returns `fetch_request_once` with an exact typed
   Drive request reference and (after turn one) the predecessor receipt reference.
   Read metadata and materialize the raw canonical JSON bytes using the connector's
   supported download route. Do not copy a text wrapper or invent a download URL.
   The metadata must match actual returned id/title/mime_type/parent_ids. The
   connector omits trash state; do not claim it was verified. Create/update the
   local evidence manifest described below.
5. Run `input --manifest MANIFEST --seq N`. Its first successful output alone is the
   model input permit. It durably burns the local exposure marker first. Verify the
   native_task_id is your admitted identity. No input replay after a crash. Perform
   the requested inference **in this actual native context** exactly once.
6. For `text_only`, write the actual answer to a private text file and run
   `result --seq N --text-file ANSWER_FILE`. For `responses_tools`, write a JSON
   result and run `result --seq N --result-file RESULT_JSON`:
   - final message: `{"kind":"message","text":"..."}`
   - one advertised function call:
     `{"kind":"function_call","name":"EXACT_NAME","namespace":"IF_ADVERTISED","arguments":{...}}`
   - one advertised custom call:
     `{"kind":"custom_tool_call","name":"EXACT_NAME","namespace":"IF_ADVERTISED","input":"..."}`
   Omit namespace when none was advertised. Respect the Mac user's task, approvals,
   sandbox and tool schemas. Return tool intent to Mac; do not execute the Mac's
   shell/edit task using your own connector or shell as a substitute. Actual tool
   outputs arrive through the next validated request, with matching call IDs.
7. The helper fixes result/claim/started bytes and emits `upload_once`. Upload these
   three objects to the approved folder using their exact names/MIME and private
   file paths. Verify each returned file ID, metadata and downloaded bytes. Keep
   evidence. If any response is lost, reconcile that one immutable object; do not
   repeat uploads or inference blindly. The helper will not automatically reissue
   an upload packet.
8. Read fresh control and run `tick` with all exact evidence. It prepares the result
   CAS. Apply/accept as above. A result commit whose response was lost may later be
   recognized in the operation ledger without issuing any native permit.
9. Wait for the controller's delivery receipt. A new request must link the exact
   preceding receipt. Repeat from `poll` with the same native identity and journal.

A JSON evidence manifest is an array:

```json
[{"reference":{"object_id":"SHA256","locator":{"backend":"drive","folder_id":"FOLDER_ID","file_id":"EXACT_FILE_ID"}},"file":"PRIVATE_RAW_JSON_PATH","metadata":"PRIVATE_METADATA_JSON_PATH"}]
```

Keep entries unique per reference. Cache verified old immutable evidence locally;
never use a folder scan or a “latest by name” search for ownership. `tick` does not
expose input content, and the only first-use input command is `input`.

## Status and recovery

`status` reports consumed phases, pending CAS, read budget and expiry. `stop` stops
local work; it does not cancel a previously issued native/tool action. Ask the
controller to CAS-close for cross-endpoint fencing.

If status says `await_exact_cas_response`, `execution_outcome_unknown`, or
`reconcile_uploads`, preserve all files and report the exact request/operation and
missing evidence to the parent. Never fabricate an answer for an unknown execution.
A known actual answer already saved may be uploaded/committed after close/expiry;
it must not be regenerated. A missing runtime cannot be rebuilt from the pin.

Do not promise the user that resource access, long uptime, latency, or actual Mac
provider/tool behavior was verified by the offline tests. Escalate a disconnected
executor or stopped native task as a real blocker; a file arriving does not wake you.
