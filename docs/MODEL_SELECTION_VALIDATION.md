# Model/effort selection validation and limits

Current policy note: the reviewed v2 catalog offers 20 bridge pairs, with xhigh as the default for every model and max rejected. Native capability observations do not make max a selectable bridge choice. Old sessions must be stopped/closed with their matching package before upgrade (and Global config restored when needed), followed by explicit Settings/reselection; signed sessions are not migrated. The v1 counts and CLI parsing observations below are historical evidence, not a claim that v2 live native acceptance has passed.

The historical source change is based on published commit
`4508189d58ca641abbf5278101f65c254f67cc73`. It does not publish, deploy, sign in,
create Google resources, or invoke a real native inference worker.

## Evidence levels

1. The versioned capability snapshot is pinned by canonical SHA-256. It records
   the observed native tool's choices, with a narrower bridge effort set where
   the official CLI preserves the requested value.
2. The trusted parent reserves one native admission plan, submits its exact
   `collaboration.spawn_agent` arguments, then records the actual successful
   returned task name. Offline tests use a clearly synthetic callback.
3. V3 bootstrap HMAC, the deployment hash, Control binding, durable journals,
   worker permits and per-request validation bind that recorded choice.
4. The native service does not return independently verifiable backend-model
   telemetry here. Every admission receipt retains
   `underlying_model_verified=false`. A caller-authored receipt alone cannot
   prove an actual backend model; the trusted parent is an explicit trust boundary.

## Offline checks

Final full-source regression on 2026-10-01: **519 Python tests + 25 Node tests passed**
(134 core, 250 remote, 47 CAS, 48 Drive, 3 actual-SDK offline, 37 guide/helper).
The 41 new selected-routing/catalog/launcher tests are included in that total.
A separate independent adversarial review passed 20 additional cases; all final
code files match the independently reviewed hashes. Catalog loading was also
checked with the actual official CLI for all 25 pairs, without inference.

The new tests cover all 25 advertised pairs, invalid/unsupported efforts,
missing and inherited native overrides, mismatched admission identities,
capability/version tampering, signed bootstrap selection and downgrade,
immutable selected pins, stale or altered permits, generic rebind rejection,
model/effort changes before request publication, and full-history tool output
continuation without a second execution.

Selected request validation runs before facade delivery confirmation and normal
controller publication. A hostile object discovered by the connector after its
existing CAS begin may consume that permit; it still cannot expose plaintext to
inference or create a result. The one-use boundary is not rolled back.

The final regression summary and exact file hashes are included in the release
manifest/validation evidence. Historical guide PDFs, DOCX and HTML remain
byte-identical to the published baseline; the new model instructions are in
[MODEL_SELECTION.zh-CN.md](MODEL_SELECTION.zh-CN.md).

## Official CLI checks

The installed official `codex-cli 0.159.2` successfully loaded all 25 generated
pair-bound catalogs using `debug models` and an isolated empty `CODEX_HOME`.
Each catalog exposes one exact model and one exact effort; Responses Lite,
effort-update support and extra multi-agent/speed choices are disabled.
The upstream fallback instruction text is preserved byte-for-byte and the
upstream license/NOTICE are included.

A strict-config check found and removed the unsupported
`model_supports_reasoning` override. The resulting CLI commands parse, but an
attempted loopback wire capture is blocked by this cloud environment's local
filesystem sandbox/socket-directory check before HTTP. Therefore the actual
emitted `/responses` request, interactive `/model` menu, real Mac/Google pairing,
real native selection and multi-hour operation have **not** been verified by
this change. Do not confuse catalog parsing with these live acceptance stages.

## Historical v1 supported semantics

- Exact session-start choice: five catalog models, efforts
  `low`, `medium`, `high`, `xhigh`, `max`
- `ultra` is rejected because CLI 0.159.2 normalizes it; `none`, `minimal` and
  `persistent` are not advertised
- One actual native worker and one model/effort pair per session
- No in-session hot switch, history rewriting, pin rewriting, tool replay,
  runtime failover or silent model fallback
- A different pair needs a fully new paired session with the old history kept
  under its original binding
- This feature applies to `remote_transport` and Router; the original frozen
  POSIX bridge is unchanged
- Legacy V2/no-selection sessions remain explicitly model-unverified

Follow the [upgrade notes](ROUTER_UPGRADE.zh-CN.md) and
[model selection guide](MODEL_SELECTION.zh-CN.md) for rollout. Both endpoints
must use the same complete reviewed source version.


## Subsequent live-acceptance defect: Router source-hash contract

A real pairing attempt reached its first REQUESTED state but failed locally while
generating the claim/begin cell. The materializer recorded six source hashes,
while the generator duplicated an outdated four-file exact allowlist. The earlier
offline checks missed the positive materialize-to-cell boundary.

The isolated fix changes only connector_cell.py in production: consume the same
complete six-file hash function as the materializer. It does not remove the two
model-binding files, relax integrity validation, or update active runtime records.
The fixed tree passed **525 Python + 25 Node tests**, including six new integration
methods and per-file/per-phase tamper cases. Its positive tests failed on the
published baseline before the fix, then passed on the fixed source.

The generator itself is pinned, so this change requires a fresh paired session;
there is no safe in-place rehash migration. See the
[source-hash fix and recovery guide](ROUTER_SOURCE_HASH_FIX.zh-CN.md). Generated-cell
and fake-connector integration tests are not evidence of a fresh live rollout.
