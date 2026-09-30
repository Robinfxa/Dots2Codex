# Validation summary

This public summary contains no private deployment paths, task identities, raw
requests, credentials, lease capabilities, or private audit reports.

## Current distributable checks (2026-09-30)

From a fresh checkout, with `jsonschema` 4.x available:

```sh
python3 -c "import jsonschema"
python3 -B -m unittest discover -s tests -v
python3 -m compileall -q .
```

The release suite has 134 tests: 46 original implementation/independent tests,
13 nonce-tool tests, 11 routing tests, 29 independent routing tests, 13 repository
loop tests, 17 independent repository loop/helper tests, and 5 release-packaging
checks. All 134 passed with no skips in the clean release verification. Counts
include overlapping coverage and do not represent independent security guarantees.

The clean-copy run has no sibling source directories or PYTHONPATH dependency.
Packaging checks cover fresh freeze creation, exclusion of private/runtime/virtual
environment files, shell-quoted helper paths, reproducible generated schemas,
scope-specific request limits, and repository-review handoff metadata. Static
compilation and scans of the publish file list are separate release checks.

Text-only runtime is standard-library-only. Both optional tool scopes and the
complete test suite require separately installed `jsonschema` 4.x; tool schema
validation fails closed when unavailable. The tests are offline synthetic fixtures:
they do not launch Codex, fetch GitHub, or prove native inference or GUI rendering.

## Sticky-routing live observations

A bounded Linux GUI run on the preceding reviewed implementation observed:

- Two simultaneous CLI/facade sessions with distinct private queues and dedicated
  native inference workers
- Session A: two text turns on generation 1, then a third text turn on a freshly
  admitted generation 2 after the original worker closed while idle
- Session B: one actual CLI nonce command and one model receipt/final, both on the
  same generation 1 worker; the final matched the reversed actual command output
- Five completed/delivered model jobs across three actual worker admissions
- Actual GUI responses observed separately from durable file-state checks
- Normal CLI exit, facade cleanup and worker-close observations

This demonstrates the bounded tested routes and that ordinary turns need no
additional model-router admission. It does not establish unattended scheduling,
arbitrary tools, hostile-user isolation, or real native-task crash recovery.

## Repository-review live result: blocked

The actual GUI repository-review attempt used one worker and made one fixed helper
`tree` call through the desktop CLI. The helper exited 1 with `network_unavailable`.
Two model requests were completed/delivered: tool intent, then a factual blocked
final. No tree, pinned repository commit, or repository file content was obtained.
The required successful tree plus three read/search calls was not reached.

Normal CLI/facade exit was recorded and worker closure was checked separately.
The exact underlying DNS/proxy/sandbox cause was not established. No broader
network permission or alternate fetch path was enabled. Offline successful-loop
fixtures validate admission/correlation logic, not successful live repository
research or summary quality.

The publication copy adds portability and packaging corrections after those live
observations: dependency diagnostics, a shell-quoted helper path, a fresh freeze
directory and exclusions, generated scope contracts, and self-contained tests/docs.
Those changes were regression-tested; a new live GUI run of this publication
copy has not been performed. The prior experimental source copies remain frozen.

## Previously observed integration evidence

In a controlled Linux environment, the project observed:

- An official Codex `0.159.0-alpha.7` process using an isolated custom-provider configuration
- A desktop facade and a native broker in different PID, network and mount namespaces, exchanging data through a private shared directory
- Two text turns through the earlier launcher, followed by one text request through the portable deployment entrypoints
- Real native text responses, queue completion and delivery, visible CLI output, normal exit and service cleanup for the portable run
- A new worker following only the documented deterministic handoff procedure, without prior project context or source inspection

The deterministic handoff is separate from native inference: it has no HTTP consumer and correctly ends at queue `completed` with `delivery=waiting`. The live portable run had four durable outbox records and no parent acknowledgment files. Native task completion was observed through a separate supported task channel; writing outbox files was not demonstrated to send a notification.

## Limits and evidence handling

Run-specific freeze manifests are generated locally with `freeze_routing.py` and
are excluded from publication. They detect accidental source changes during a
run, not authenticity of the code or of a native platform identity.

`verify_routing_live.py` and `verify_repo_review.py` are read-only file-state
checks. Their `--require-cleanup` / `cleanup_verified` fields cover desktop CLI
exit and facade shutdown only. Verify durable worker closure separately with
`routing.py status`, and use the actual native task tools to establish task
termination; a closed file record alone cannot prove the platform task ended.
Neither verifier establishes GUI pixels or native task identity by itself.

The HTTP facade buffers a complete result before emitting SSE; it is not live
token-by-token model streaming. Unknown inference, delivery or execution outcomes
must be resolved with actual evidence and never replayed speculatively. Same-user
processes with write access are inside the cooperative trust boundary.

No generic tool agent, daemon, 24/7 uptime, Windows support, user macOS deployment,
public HTTP service, automatic authentication, automatic file-to-agent wake,
free inference entitlement or exactly-once remote dispatch is established. Private
raw evidence is not distributed. Verify all actual platform/CLI capabilities and
permissions in the target environment before using an optional scope.
