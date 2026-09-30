# Experimental Docs-CAS control and direct-connector smoke

For endpoint download, explicit authorization, provided client factories and
blank-document initialization, start with [remote setup](docs/REMOTE_SETUP.zh-CN.md).
The [public validation summary](docs/REMOTE_VALIDATION.md) records the completed
same-principal connector/native test separately from unverified remote CLI setup.

## Correction and scope

Ordinary Drive blob APIs in the current connector do not expose conditional
create/update, but the connected Google Docs API DOES expose
`get_document.revisionId` and `batch_update_document.write_control.requiredRevisionId`.
The earlier blanket “connector has no CAS” assessment was too broad.

`MessageStore` transports immutable blobs. `SessionControlStore` controls admission,
claim, dispatch intent, result commitment and observed delivery. The experimental
`GoogleDocsCASControlStore` uses one pinned document, one pinned tab, and one unique
canonical control block. `targetRevisionId` is never used: it merges edits rather
than rejecting stale writers. Revisions are opaque, user-bound, valid for a limited
period (officially guaranteed for 24 hours), and never treated as epoch counters.
Each writer gets state AND revision in one fresh read under its own authenticated
identity. This implementation accepts snapshots for at most 30 seconds.

A stale required revision means the batch was not processed. Only an explicitly
identified stale-revision rejection is a CASConflict; arbitrary 400 responses,
timeouts, malformed success and missing replies must not be relabeled that way.
All conditional updates use a literal full-block replacement excluding exactly
the document's mandatory terminal newline from both match and replacement, `matchCase=true`,
`searchByRegex=false`, and `tabsCriteria.tabIds=[pinnedTabId]`. Exactly one numeric
`occurrencesChanged` and the expected response document ID are required. A successful
HTTP response with no match, an omitted count, or merely a changed revision grants
no ownership or execution permit.

The parent's separately authorized live primitive test has observed one winning
same-revision update and one explicit stale loser, successful multiline matching,
and a successful no-match response whose count was omitted. Those findings support
the primitive only; the complete connector-native roundtrip is a separate gate.
No account IDs, current revision IDs, or private live-resource IDs belong in source.

## Data and authority

The control record carries typed request/result/receipt locators and hashes. Drive
locators contain the exact folder ID and file ID. Result records also retain the
request, claim and started dependency locators. Readers fetch exact IDs from the
known control document; no folder listing, name search or newest-file heuristic is
needed. Full canonical bytes, content hashes, complete deployment identity, sequence,
predecessor receipt and result graph are checked before accepting a transition.

The normal raw REST backend checks file ID, parent list, name, trash state and raw
bytes. The actual native connector normalizes metadata to `id/title/mime_type/
parent_ids` and omits `trashed` even when requested. The direct-connector smoke's
`ConnectorEvidenceMessages` checks that verified normalized schema, the dedicated
folder, exact ID/name, raw JSON MIME and bytes/hash, and reports
`trash_state_verified=false`. It does not invent a trash-state check or claim the
native connector conforms to every raw-REST capability. Deletion is not cancellation
in this protocol; execution authority comes from the control record.

Claim and DISPATCH_INTENT are separate CAS transitions on the SAME record. Rebind
and begin compete against that same record. If begin wins, rebind is forbidden;
if rebind wins, the stale begin cannot commit. Rebinding before dispatch retires
that request and requires a fresh deployment pin, increasing generation, a different
native task and worker journal. It never migrates an uncertain execution. Unknown
begin writes and ambiguous native results block replay. A retained operation-ID
ledger recognizes an already-applied operation but NEVER recreates its native permit.
No ledger pruning or automatic failover is implemented.

Control bindings retain deployment expiry. Fresh admit/claim/begin/rebind enforce
it; if begin commits while its response crosses expiry, the intent remains but no
permit is returned. Known results/receipts may be recorded afterward. The document
is dedicated and has no human editing, suggestions, extra tabs or other writers
outside this protocol. SUGGESTIONS_INLINE and no suggested changes are required.
The adapter accepts both official raw tabs and the verified flattened connector
tabs; ambiguous structures halt. Document undo/rollback or malicious editors are
outside the trust boundary, not something hashes or revisions can defeat.

## Runnable code

The original single-writer facade/worker commands remain available. They are NOT
silently CAS-enabled. Explicit CAS selection adds ALL of:

```text
--docs-client-factory reviewed_docs_module:create_client
--control-document-id DOCUMENT_ID --control-tab-id TAB_ID
--control-id CONTROL_ID --control-writer-identity AUTHENTICATED_WRITER_LABEL
```

These options instantiate CASController/CASWorker and control-reference discovery.
Enrollment is durably pinned in each fresh role journal. Omitting the CAS flags
later, or reusing an already-created plain actor, fails closed; the old unfenced
commands cannot operate an enrolled journal. Existing used single-writer journals
are not silently upgraded.
For Drive use `--transport drive --folder-id FOLDER_ID --client-factory
reviewed_drive_module:create_client`, normally with `--drive-mode duplicate_tolerant`
when generated-ID creation is unavailable. The client factories require independently
authorized API clients; connected-plugin credentials are never extracted or copied.
The public Python entrypoints are CASController, CASWorker, SessionCoordinator and
GoogleDocsCASControlStore. The control store's low-level CAS method is a storage
primitive; legal state transitions must go through SessionCoordinator.

Control-aware worker startup reads REQUESTED from the pinned control document,
then persists its local dispatch marker, CAS-claims and CAS-begins before returning
input to native execution. A lost or raced CAS burns availability rather than
issuing another permit. Results are displayed only after CAS result commitment;
delivery receipts advance the same record. The existing confirmed-client-history
and explicit final-ack rules remain in force.

## Direct native connector smoke: parent owns writes until delegation is authorized

This is a bounded synthetic text roundtrip through real Drive/Docs tools and a real
native worker. It is NOT a desktop daemon, independent-machine test, automatic
wake, general tool executor, or proof of exactly-once external side effects.
The request body must reach the worker only through the Drive file, not its spawn
prompt or follow-up message. Use only the separately approved synthetic folder/doc.

### 1. Admit the native worker and prepare packets

Use the actual supported native task tool to create/park a worker with instructions
to wait for the control configuration; do not include the request text. Record its
actual returned task identity. In a fresh private runtime directory:

```sh
python3 -m remote_transport.connector_smoke init --root RUNTIME \
  --session synthetic-session --native-task-id ACTUAL_NATIVE_TASK_ID --control-id UNIQUE_CONTROL_ID
```

This writes the immutable pin, full `control-block.txt`, and
`control-insert-text.json`. Initialization does NOT verify native admission: the
parent must match the actual tool-returned task identity. Copy only the exact pin
to the worker's fresh private runtime directory. Keep controller prompt files out
of the worker instructions and paths.

### 2. Initialize the already-approved dedicated control document

Directly call `mcp__codex_apps__google_drive_get_document` with the exact document ID
and fields `documentId,revisionId,suggestionsViewMode,tabs`. Save the complete
structuredContent privately. Verify the expected single tab and synthetic contents.
When replacing the earlier synthetic primitive fixture, atomically delete only its
body text using the fresh exact indexes (preserve the final mandatory newline),
then insert the `control-insert-text.json` text with the pinned tab ID. Use
`write_control:{requiredRevisionId: freshly_read_revision}` on that initial batch.
The insert text deliberately omits its trailing newline: Docs supplies the existing
final newline. An extra blank paragraph fails the exact-block guard. Actual Docs replacement
was verified to preserve the mandatory final newline: prepare_update therefore
uses snapshot.block[:-1] and replacement[:-1]. Never strip arbitrary whitespace or
relax the exact full-document parser. If an older test plan introduced one extra
blank paragraph, repair only that known synthetic extra newline under a fresh
requiredRevisionId, then verify the exact canonical block before continuing.

Read the full document again. The reconstructed text must equal `control-block.txt`
exactly and suggestionsViewMode must equal SUGGESTIONS_INLINE. Initialization is
not a claim or dispatch and grants no native permit.

### 3. Publish and verify the request by exact file ID

Create a private text file with one synthetic request, then:

```sh
python3 -m remote_transport.connector_smoke request --root RUNTIME --text-file REQUEST_TEXT_FILE
```

The command prints the canonical filename, file path and object hash. Use the real
`mcp__codex_apps__google_drive_upload_file` with its supported local file_uri,
that filename, MIME application/json and the approved parent_folder_id. Capture
the actual returned ID. Directly call `get_file_metadata(fileId, fields:
'id,name,mimeType,parents,trashed')` and `fetch(url: canonical file URL,
download_raw_file:true, include_base64:false)`. Materialize the complete raw bytes
through the returned supported authenticated file_uri/workspace_path, then verify
the consumer-local file exists and is readable. Use the connector's supported authenticated materialization/download route, verify
that the returned path exists in the consuming environment, and compare exact
bytes/SHA-256 to the uploaded canonical source.
Keep the local evidence private (mode 0600). Never expose its private download URL; do not copy a readable-text
wrapper, reformat JSON or guess a download URL. If exact bytes cannot be obtained,
stop before native dispatch. Do not fall back to inline base64 without an
explicitly requested compatibility flow.

Save each real metadata structuredContent and raw byte file. A typed reference is:

```json
{"object_id":"SHA256","locator":{"backend":"drive","folder_id":"FOLDER_ID","file_id":"RETURNED_FILE_ID"}}
```

An evidence manifest is a JSON array of entries:

```json
[{"reference":{"object_id":"SHA256","locator":{"backend":"drive","folder_id":"FOLDER_ID","file_id":"FILE_ID"}},"file":"ABSOLUTE_RAW_FILE","metadata":"ABSOLUTE_SAVED_METADATA_JSON"}]
```

The config JSON contains document_id, tab_id, control_id, writer_identity and folder_id.
Do not put any token/password in it. Save private runtime JSON with mode 0600.

### 4. Prepare and apply the CAS admission

Read a fresh document snapshot directly through the connector. Save operation
arguments `{"request":REFERENCE}`. Then generate the exact API call locally:

```sh
python3 -m remote_transport.connector_smoke plan --root RUNTIME --kind admit \
  --snapshot GET_DOCUMENT_JSON --config CONFIG_JSON --manifest EVIDENCE_MANIFEST \
  --arguments ARGUMENTS_JSON --operation-id UNIQUE_ADMIT_OP --save PLAN_JSON
```

Planning validates inputs and produces no write or native permit. Take only
`PLAN_JSON.tool_arguments` and pass it unchanged to the direct
`mcp__codex_apps__google_drive_batch_update_document` tool. Save its full result,
read back the document, and run:

```sh
python3 -m remote_transport.connector_smoke verify --root RUNTIME \
  --plan-file PLAN_JSON --response BATCH_RESULT_JSON --readback FRESH_GET_JSON
```

An explicit stale-revision error requires a fresh read and legal re-plan. An unknown
outcome requires operation-ID reconciliation and never a new native attempt.

### 5. Worker claims and begins without receiving the request out-of-band

Give the native worker the trusted pin, control document/tab/control IDs, approved
folder ID and this procedure. Do not give it request contents. It reads the pinned
control document and obtains the exact request reference. Before fetching the
request body, generate claim/started envelopes from the hash alone:

```sh
python3 -m remote_transport.connector_smoke claim-started --root WORKER_RUNTIME --request-id REQUEST_OBJECT_ID
```

Upload/readback-verify these two immutable envelopes as above. Their publication is
not execution authority. Use binding from the verified control record and unique
claim/operation IDs. Claim args are `{"binding":BINDING,"claim_id":"CLAIM_ID"}`.
Begin args are `{"binding":BINDING,"claim_id":"CLAIM_ID","dispatch_id":"HASH_FROM_CLAIM_STARTED_COMMAND"}`.
For each transition, read fresh, plan, invoke the direct Docs batch tool, and verify
its exact response plus readback. The offline verifier additionally requires the
fresh response writeControl.requiredRevisionId to equal that same-principal
readback revision and differ from the plan's input revision. An earlier success
file or an intervening edit cannot stand in for the actual begin response. Neither claim nor a merely observed old begin
permits execution. For the fresh successful begin only:

```sh
python3 -m remote_transport.connector_smoke consume-begin --root WORKER_RUNTIME \
  --plan-file BEGIN_PLAN --response FRESH_BEGIN_RESPONSE --readback BEGIN_READBACK
```

This persists a local one-use marker before returning the request reference. A
second consume fails. Missing runtime state, unknown begin response, marker loss,
expiry or control disagreement means stop, not rebuild a new journal. Only after
this fresh consumption may the admitted native worker fetch/validate the request's
actual raw bytes and perform the single text-only inference.

### 6. Publish the actual native result, then confirm observed delivery

The worker writes its actual answer to a private text file and prepares the result:

```sh
python3 -m remote_transport.connector_smoke result --root WORKER_RUNTIME \
  --request-file FETCHED_REQUEST_JSON --started-file VERIFIED_STARTED_JSON --text-file ACTUAL_ANSWER_FILE
```

Upload and readback-verify it. Result transition args contain binding, dispatch_id,
and typed references named request, claim, started and result. The planner fetches
all four from saved real raw bytes/metadata evidence and checks the entire chain.
Apply and verify that result CAS. The parent then reads the exact committed result
and dependency IDs from the control document, fetches/verifies them, and observes
the actual answer. Only then create the receipt:

```sh
python3 -m remote_transport.connector_smoke receipt --root RUNTIME \
  --request-id REQUEST_OBJECT_ID --result-file FETCHED_RESULT_JSON --evidence "Parent observed the verified native answer"
```

Upload/readback-verify it. Receipt args are `{"binding":BINDING,"receipt":REFERENCE}`.
Apply and verify the final CAS. Report all resource IDs, hashes and actual native
identity to the parent privately; publish only redacted counts/claims. Retain the
synthetic evidence. Do not delete or share resources without authorization.

The helper only prepares and verifies local files. It does not call connectors,
spawn workers, transfer authorization, or let a stored CAS success recreate a
consumed execution. Plain `worker-next` without explicit CAS flags is still the
single-writer baseline and must not be used as the CAS smoke's execution gate.

## Sources

- [Docs batchUpdate and writeControl](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [Document revision IDs](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents)
- [Full document reads](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get)
- [Docs usage limits](https://developers.google.com/workspace/docs/api/limits)

The implementation bounds requests and operations; production poll intervals must
also fit Docs' per-user/project read and write quotas. No high-frequency background
poller is enabled by this prototype.
