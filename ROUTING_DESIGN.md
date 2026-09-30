# Sticky routing v1 (bounded experimental extension)

## Decision

One private deployment, queue, loopback facade and dedicated native inference
context per logical session. A small file-backed control plane allocates the
worker only on initial admission or explicit safe failover. It never sees or
classifies normal prompts and is not a model call. The facade sends every normal
request, including tool receipts, directly to the existing session queue. The
bound worker waits there and calls the deterministic data adapter.

The same owner can run multiple isolated sessions concurrently. Each individual
queue still admits one request at a time and preserves request order. A worker
native task identity is never reused for a different session or generation.

## Identity and admission

The session key is chosen at provisioning time; deployment_id and run_id are the
authoritative durable identity. Each session has a separate endpoint. Provider
request IDs and prompt_cache_key never allocate or select a route. The preserved
service adapter pins one canonical CLI session-id/thread-id pair to that endpoint.
This is a single-user local trust boundary, not multi-tenant authentication.

`create-session` durably reserves generation 1 and an admission ID, worker ID,
random worker instance, and assignment ID. Before using a REAL supported native
task tool, the authorized parent adapter calls `dispatch-started`. After it gets
the real task identity, it calls `confirm` with that exact identity. Python never
spawns inference. A file does not wake the parent; parent/operator must be active.
Unknown admission outcomes remain uncertain until the actual platform outcome is
established. A definitely rejected/not-started admission may be retired with an
explicit evidence reference and retried within a three-attempt budget.

The binding intent records its exact reserved broker assignment tuple before
calling the existing assignment API. Confirmation retries can adopt only that
exact assignment. A crash after assignment cannot allocate a second worker.

## Deterministic data plane and locks

All routed claim/read/renew/complete/close operations hold the registry lock, then
the deployment lock, then queue lock. Claim waits outside locks and claims only
with wait=0 inside. The registry never holds locks while native inference runs.
Every operation validates registry/deployment/session/worker instance/generation,
assignment ID+epoch, live binding and worker lease. A routing marker makes the old
unfenced broker CLI fail closed for routed sessions; legacy deployments remain
unchanged. These checks are accidental-stale-worker defenses inside the same-user
trust boundary, not secrets that defeat a malicious process with file access.

## Failover

Only an explicitly closed or expired worker can be replaced. No lease resurrection.
Generation increments, so all old tickets/credentials are rejected. Pending and
claimed-but-never-read requests may continue only after their old queue lease has
expired. A recorded inference start whose completion is unknown blocks failover,
even if the HTTP request was canceled or its deadline expired. A durably completed
result is not inferred again. Missing/ambiguous HTTP delivery blocks failover.

A reserved tool intent remains an unresolved execution until a strictly correlated
successful receipt has actually arrived in the queue. Without it failover is
blocked. No tool execution is automatically replayed. The optional scopes permit
only one nonce command or the fixed repository-read helper; neither is a general
shell/tool executor. Repository review explicitly forbids worker replacement. A healthy worker can keep
processing its own receipt and final response without involving the router.

## Bounds and recovery

Registry/session lifetimes, session count, admission attempts, model requests,
lease durations and waits are bounded. Initialization interrupted before the
session manifest/route commit fails closed. Reopening a committed registry restores
bindings; reopening a facade/CLI deployment still obeys the old no-replay rule.
There is no permanent scheduler, automatic wake-up, or inferred authorization.
