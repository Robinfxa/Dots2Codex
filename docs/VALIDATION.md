# Validation summary

This summary is intentionally redacted. It contains no private deployment paths, agent identifiers, request transcripts, lease capabilities or account data.

## Public test suite

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

The public suite contains 24 implementation tests and 22 independently developed regression tests. Their coverage overlaps; the count is not a claim of 46 independent security guarantees.

It covers private fresh directories, generated deployment identities, a shared-file nonce handshake, a synthetic loopback HTTP/SSE response, concurrent role admission, owner/version/hash checks, lease and epoch fencing, safe takeover before inference begins, ambiguous dispatch fencing after inference begins, exact-result idempotency, durable stop causes, crash-boundary fault injection, outbox acknowledgment validation, bounded restart budgets, and schema instances.

The runtime uses only Python's standard library. One schema-conformance test uses `jsonschema` when already installed and reports a skip if it is absent. Tests do not install dependencies or launch an official Codex executable. The HTTP test uses a short-lived local synthetic fixture.

## Previously observed integration evidence

In a controlled Linux environment, the project observed:

- An official Codex `0.159.0-alpha.7` process using an isolated custom-provider configuration
- A desktop facade and a native broker in different PID, network and mount namespaces, exchanging data through a private shared directory
- Two text turns through the earlier launcher, followed by one text request through the portable deployment entrypoints
- Real native text responses, queue completion and delivery, visible CLI output, normal exit and service cleanup for the portable run
- A new worker following only the documented deterministic handoff procedure, without prior project context or source inspection

The deterministic handoff is separate from native inference: it has no HTTP consumer and correctly ends at queue `completed` with `delivery=waiting`. The live portable run had four durable outbox records and no parent acknowledgment files. Native task completion was observed through a separate supported task channel; writing outbox files was not demonstrated to send a notification.

## Limits

The integration observations are bounded examples, not a compatibility or uptime guarantee. They do not establish a generic tool-execution loop, Windows support, macOS deployment, public network access, automatic authentication, unattended native-agent scheduling, real native-task crash recovery, or exactly-once remote task dispatch.

The HTTP facade buffers a complete result before emitting its SSE response. It is not token-by-token model streaming. Recovery and failure paths were tested synthetically; a result marked as uncertain must still be resolved using actual platform evidence before dispatching another inference task.

Private raw evidence is not part of the public distribution. The tests are reproducible locally; external platform capabilities and CLI behavior must be verified in the environment where the project will run.
