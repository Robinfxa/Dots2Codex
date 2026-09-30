# Remote transport validation

This public summary omits private resource IDs, account names, native task IDs,
request/answer contents, runtime paths and raw provider responses.

## Live long-session baseline (2026-09-30)

The pre-optimization long-session source tree
`91aad78505d4d770f6a6e770ad6c8a62136941c9bb97af658dd62cdd9c82b4a2`
completed a bounded, approximately 28-minute session through an actual Mac
controller, Google Drive/Docs, and an active native worker. All four requests
reached `DELIVERED`. Request 3 emitted a native function-tool intent; request 4
contained the matching Mac `function_call_output` with exit 0, followed by native
continuation, committed result, and validated controller receipt.

At 23:09 UTC, the controller CAS closed the session at epoch 21 with
`closed=true`, phase `DELIVERED`, and `execution_may_be_running=false`. The worker
then confirmed `stopped=true`, 89 reads, and no pending operation. No active
worker or session remains from that test.

This proves the observed bounded four-request/tool-continuation path, not
multi-hour uptime, complete 128-request capacity, general cross-OAuth-app
interoperability, or exactly-once external effects. The new parallel connector
executor was not used in that live session and has **offline validation only**.
Its 1.95× virtual-delay benchmark is not a production speedup measurement. See
[latency validation](LATENCY_VALIDATION.md) and [long-session setup](REMOTE_LONG_SESSIONS.zh-CN.md).

## Earlier remote release relationship (historical)

The new remote package is merged alongside the existing local bridge at public
baseline `d8ba399551a560b0050ff88941d06c58448cae56`. Existing runtime code, tool
scopes, schemas and local tests remain unchanged. The previous README is retained
as `LOCAL_BRIDGE_README.zh-CN.md`; the root README now covers both paths.

All 12 remote runtime modules and 8 original remote developer test files match the
source used for the corrected live test. New setup examples and their tests are
separate additions, with offline-only verification. Documentation was adapted for
publication; private experiment trees and raw evidence were not published.

## Earlier one-request connector/native roundtrip (2026-09-30)

One bounded text request passed through actual Drive/Docs connector operations and
one freshly admitted native worker, then returned to the controller:

`admit -> claim -> begin -> result -> receipt`

The final control record was `DELIVERED` at epoch 5. The recorded checks covered:

- Five actual immutable message objects with exact file-ID references, canonical
  raw bytes, hashes and deployment identity
- Complete request/claim/started/result dependency graph and matching receipt
- Fresh conditional claim, begin, result and receipt writes, each with exactly one
  replacement and matching immediate readback
- A one-use begin marker bound to the admitted worker and dispatch
- A request delivered through Drive, without its contents in the worker handoff
- Controller result bytes matching the worker's actual answer and synthetic challenge
- Actual parent observation before the final delivery receipt

The independent evidence review reconstructed legal transitions and hashes from
saved provider responses and downloaded bytes. It did not repeat cloud reads or
independently establish historical execution ordering from files alone.

### Disclosed pre-START correction

The first admission produced an extra paragraph because real Docs retains its
mandatory terminal newline. Strict readback validation stopped before native
START. The API matching/replacement strings were corrected to omit exactly that
terminal newline; the canonical full-document parser stayed strict.

Only the known synthetic extra blank was manually removed under a fresh revision.
The repaired document still matched the original REQUESTED admission candidate,
so admission was reconciled rather than replayed. Fresh claim/begin/result/receipt
then passed. This is not evidence of general automatic crash recovery or repair.

## Earlier remote release checks (historical)

Run the commands in the root README. Coverage groups are reported separately
because their purposes overlap:

- 134 preserved local bridge/routing/tool/release-packaging tests
- 73 frozen remote developer tests
- 18 new endpoint factory/setup tests
- 47 independent Docs-CAS/control/reference/smoke tests
- 48 earlier independent Drive/facade/CLI tests

Three optional SDK-specific audit tests additionally use the actual installed Google
packages (versions recorded in requirements-google-example.txt) with network access blocked: static factory creation, exact request/body
shape and an underlying lost-reply retry. Source compilation, clean-copy tests,
public-file scanning and remote publication checks are separate verification steps.
No offline suite performs live authentication or native inference.

The SDK wrapper disables application status retry and auth-failure replay, but
httplib2 may resend the identical request after a socket failure. The unchanged
requiredRevisionId fences that resend. A lost first success followed by stale
failure is conservatively unknown; it cannot create another native permit.

## Not established by that earlier one-request release

- Independently deployed Codex CLI + remote facade + native worker across machines
- User endpoint OAuth setup or cross-principal/cross-OAuth-app file interoperability
- Live use of the new standalone SDK factories or blank-control initializer
- Real CLI header/profile/readiness compatibility and exact assistant-item ACK behavior
- Raw REST pagination/create-by-ID behavior against the live service
- General tool execution, automatic failover, unattended wake or permanent scheduling
- Exactly-once external effects, safety under malicious writers, journal cloning,
  rollback, missing durable state or arbitrary human editing of the control Doc

The actual live test used one connected principal. Connector metadata omitted
trash state, so that specific check was not claimed; the standalone Drive port has
its own metadata contract. Drive file arrival cannot wake dot or create a worker.
Google scopes, ACLs, authorization, native platform permissions, sandboxing,
approvals, terms and usage limits still apply.
