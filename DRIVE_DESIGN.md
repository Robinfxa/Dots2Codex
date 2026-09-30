# Drive transport design, v0 experimental

## Current phase update

The original single-writer baseline below is preserved as a distinct mode. A later
verified capability adds opt-in Google Docs requiredRevisionId CAS control, typed
known-file-ID message references, and CASController/CASWorker wrappers. See
[Docs-CAS design and live-smoke runbook](DOCS_CAS_RUNBOOK.md). Ordinary Drive blob
APIs still lack the needed conditional primitive; the connected Docs API supplies
one. No native exactly-once or automatic uncertain-execution failover is claimed.

## Decision before implementation

Keep the frozen POSIX implementation byte-for-byte. The new `remote_transport`
package is a separate object protocol with `TransportBackend`, `LocalFSBackend`
(for conformance testing only), and `GoogleDriveBackend` (independent API port).
Pointing `BRIDGE_ROOT` at a Drive-synced directory is neither implemented nor safe.
The old facade is not silently switched to this experimental backend. A separate
`RemoteResponsesFacade` reuses its frozen HTTP/SSE handler with a new remote store.

This version is a bounded, text-only, one-session transport prototype. It does not
yet provide a remote native-Codex end-to-end deployment or generic tool execution.
A separately runnable text-only facade and worker command path is now implemented
and tested offline. Actual Codex profile/readiness integration and real remote
end-to-end operation remain later validation gates.

## Authority, safety, and delivery

Provision exactly one controller host/journal and one worker host/journal for each
session, with a statically pinned deployment hash, session, worker generation,
assignment epoch, native task identity and journal incarnation. The pin must come
from trusted operator configuration, never the highest generation seen remotely.
The designated worker's native context must already have been admitted through an
actual supported native task tool. Python never invokes a model or spawns an agent.

The single-writer placement is a deployment precondition, not something Drive
files enforce. Same-host flock prevents accidental concurrent access to the same
journal. Copying credentials/journals to a second live host violates the precondition.
Supporting multiwriter assignment or automatic failover needs an external linearizable
CAS/transaction authority and a true fencing-aware execution target. Drive names,
versions, revisions, list snapshots and check-then-upload do not provide that.

All folder writers are trusted. Hashes detect corruption and accidental inconsistent
objects; actor labels and hashes are not authentication against malicious writers.
Unknown execution cannot be undone or proven absent by a timeout or missing file.

## Immutable objects

Deployment, request, claim, started, result, receipt, and ambiguity records are
canonical JSON raw blobs, with SHA-256 object IDs and independently checked semantic
slots. Each non-deployment record links the exact deployment hash, complete identity,
request ordinal, author role, and prior object hashes. Each slot has one canonical
value. Identical physical duplicates collapse; different hashes in one slot halt.
No manifest is overwritten to obtain ownership. No delete or cleanup is automatic.

The controller durably fixes request bytes before publication. One request can be
outstanding; later requests link the previous receipt. The worker only admits the
next ordinal after the preceding result and receipt chain are visible and validated.
The worker persists a claim then a started/dispatch-intent locally before returning
an invocation permit. This irreversible marker precedes the supported native-call
boundary. A second begin never yields another permit, even if started publication
or the native call's response was lost. The caller must treat the permit as one-use.
Native-task identity is pinned; passing an arbitrary callback is not a native proof.

A result is stored locally before publishing. Publication can be safely retried only
with the same fixed bytes. A result is not a client-delivery acknowledgment. Reading
one does not mean it was shown to the user. The controller must record a separately
confirmed delivery receipt, with evidence, before admitting another request. Unknown
delivery never triggers inference again. Missing/corrupt journals on resume halt.
Provisioning a new session does not recover an old journal.

## Drive capability modes

Strict physical create mode uses `files.generateIds`, persists the returned ID
before `files.create`, and retries the same ID. A 409 is reconciled by get/readback
and exact-byte verification. No new-ID fallback is allowed. Pre-generated IDs are
for raw blobs, not native Google Workspace documents.

The currently connected ordinary Drive blob APIs expose upload/get/list but no
generateIds, create-by-ID or verified conditional CAS. The separate native Docs
API DOES expose requiredRevisionId and is used by the opt-in CAS control mode. Its supported prototype mode permits physical
copies of one immutable object. Upload intent bytes are durable first. An uncertain
upload stops; bounded reconciliation may find the exact object. Explicit recovery
may re-publish those exact bytes only, never the associated native effect. Empty or
partial listings are not proof that an earlier write failed. This is semantic
idempotence under the durable-owner assumption, not exactly-once upload or execution.

The Python adapter takes an explicit caller-supplied Drive API client. A raw HTTP
implementation can use a separately authorized user-managed token provider. It
never reads browser/connector credentials, and connected plugin authorization does
not transfer to a desktop daemon. Connector-driven tests exercise storage semantics;
adding a model relay per prompt is not part of this architecture. The connected
plugin has not established complete raw-JSON pagination, so it is not certified for
the general mailbox adapter; a known-file-ID bounded smoke is a narrower test.

## Reconciliation and bounds

All pages must be read successfully; incompleteSearch, malformed pages, missing
media, rate-limit/error, excessive pages/objects or conflicting bytes stop the pass.
No mutable cursor is advanced on a partial pass. This version uses bounded full
poll/reconcile rather than assuming connector Changes support. Previously validated
objects are retained locally, so temporary omissions never roll history backward.
Objects outside the pinned filename namespace are ignored. Invalid in-namespace
objects and unexpected own-role objects without local issuance intent halt.
References can arrive out of order; an incomplete chain waits, never infers absence.
Polling uses finite attempts and capped backoff, returning pending/ambiguous at the
budget rather than manufacturing success. Lifetimes and request counts are bounded.

## Validation claims

Mocks must cover reordering, duplicates, delayed visibility, partial pagination,
429/403/timeouts, uncertain creates, content poisoning, pin mismatch, restart and
crashes on either side of the local dispatch marker/result persistence. Legacy tests
still run unchanged. A real dedicated-folder test, if separately authorized, proves
only Drive byte transport and readback across connector roles. A real independent
machine deployment additionally needs separately authorized desktop OAuth, bridge
integration, supported native admission, and end-to-end observed CLI results.

References: official [pre-generated IDs](https://developers.google.com/workspace/drive/api/guides/create-file#generate_ids_to_use_with_your_files),
[files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files),
[list](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list).
