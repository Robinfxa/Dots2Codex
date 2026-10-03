# Pinned Codex tool compatibility

Tool contract: `dots-direct-tools/2`; MCP server 0.3.0. Target client: official
Codex CLI 0.159.2, source `ff6aec96948b70d94983af2641a6b67c94faeff5`.
This is a bounded compatibility implementation, not every Responses API feature.
The global/session service shares this contract. Legacy single-route experimental
mode keeps its original eight-tool surface and has no native-hosted executor flow.

## Execution locations

| Declared type | Execution and evidence |
| --- | --- |
| `function` | Exact schema, namespace, arguments and call ID emitted to Codex; real client callback required. Includes shell, file, MCP, planning and other advertised functions. |
| `custom` | Exact input string emitted to Codex. Text/grammar declarations preserved. The owning client parser enforces its grammar, e.g. apply_patch or code-mode exec. |
| `namespace` | Exact namespace metadata and each child declaration remain discoverable by immutable schema receipt. No flattening or guessed alias. |
| `tool_search` | `execution: client` only. Emit `tool_search_call` with object arguments. Correlate the actual `tool_search_output`; revealed tools become available from its `tools` list. Empty discovery is legitimate. |
| `web_search` | Native worker executes the supported mapping below after durable reservation. It is never disguised as a local function and never waits for a nonexistent Mac web callback. |

A valid but unused web-search declaration is admitted, including the client's
normal cached declaration (`external_web_access: false`). `get_request` includes
upfront `capabilities.hosted` diagnostics. Admission does not promise every
execution option can be represented by the native tool.

Client tools still execute under Codex's own sandbox/permission/confirmation
rules. Preparation is not permission to bypass those rules or the native worker's
confirmation rules. Direct does not run shell commands, MCP tools, web queries,
or external model inference inside Python.

## Native web support and precise gaps

Supported mapping to an actually available native `web.run`:

- Search `query` / `queries` maps to `search_query[].q`; up to four distinct
  requested queries per native batch. Both captured query fields are preserved in
  the response action. Four queries use native `response_length: medium` because
  that is the native batch contract, not a retrieval-context-size emulation.
- `open_page.url` maps to `open[].ref_id`.
- `find_in_page.url/pattern` maps to `find[].ref_id/pattern`.
- `filters.allowed_domains` maps to search domains and also restricts open/find
  destinations and receipt/citation source URLs by exact host/subdomain boundary.
- Absent or true `external_web_access`, absent or false `indexed_web_access`.

The current native web schema cannot enforce cached-only search, indexed-only
fetching, approximate user location, retrieval `search_context_size`, or
`search_content_types`. Selecting one returns a specific `web_search_*_unavailable`
capability error before any native call. Unknown options also fail before use.
Nothing changes the user's web_search setting. A switch to live search requires
an explicit user decision and a new matching declaration; it is never inferred
from permission to use cached search.

`web_search_preview`, file search, code interpreter, computer and image generation
are not members of the pinned client's ToolSpec enum. They produce explicit
`unsupported_hosted_tool` errors with safe tool-type/index diagnostics. Server
`tool_search` is explicitly unsupported. Audio/video, encrypted/reasoning history,
and other generic Responses item variants remain explicit content/item errors.
Background jobs, remote response storage, automatic truncation, audio/modalities,
and unsupported generation controls (temperature/top_p/max_output_tokens)
are rejected explicitly rather than silently advertised as implemented.
Structured `text.format: json_schema` text finals are validated, including
auxiliary title-generation requests. Plain text remains supported.

## Native lifecycle

The original eight tool contracts are retained. Global mode adds two tools;
installed MCP definitions must be refreshed before using them.

1. Claim the request with the existing immutable route/worker/context identity.
2. Read capabilities. Confirm that your actual native tools include `web.run`.
   Python's mapping support is not platform availability attestation.
3. Discover `web_search`, then retrieve its exact immutable declaration through
   `lookup_schema`. Unnamed catalog keys are `["web_search"]` and
   `["tool_search"]`; they cannot collide with `[namespace,name]` client keys.
4. Call `prepare_hosted_call` with route/claim/request/context, a stable
   `operation_id`, fresh `ws_...` item ID, exact `action`, and schema tokens.
5. Only a first `execute: true` return permits one actual native invocation, with
   exactly the returned `native_tool` and `native_arguments`. Reservation is
   persisted before return. Replayed/uncertain preparations never permit a retry,
   even after restart. A pending operation prevents another reservation.
6. Call `record_hosted_result` with the operation token and a `result` object:
   `native_tool`, exact `native_arguments`, complete `native_result`,
   `status: completed|failed`, and `sources` containing exact `url`, `title`,
   native `reference`. Source URL/reference must occur in the actual result.
   Empty successful searches have an empty source list and cannot cite anything.
   Failures have an explicitly failed observation and no verified citations;
   they never emit a completed-search lifecycle event. Do not fabricate a
   result, source, citation, or native reference.
7. Include every returned web observation, successful or failed, exactly once in the
   eventual response. Include a useful final answer, or a separate client-tool
   intent if more work is needed. Completed native items don't turn a text final
   into a client-action wait. Unknown operations must be reconciled or cancelled.
8. Put descriptive links to verified source URLs directly in answer text.
   Pinned Codex drops citation annotations in its typed text representation;
   annotations can additionally be supplied for receipt validation, but cannot
   be relied upon as displayed links. Never expose native `turn*` reference
   markers as user-facing citations. Prior verified source URLs remain available
   for later answers in the same route without repeating the search.

All records bind route, claim, request, context, exact declaration hash, action,
and native mapping. Record retries must be identical. These are trusted-worker
reports, not cryptographic proof of platform execution. Status flags explicitly
retain that limitation. Cancelling a request with a reservation reports its
native effect as unknown; cancellation cannot undo a web request.

SSE is buffered after a native result has been recorded. It includes completed
web observations and web lifecycle events; the timestamps do not claim live
streaming of the original native search. Pure completed observations can be
replayed without another native execution. Client execution intents retain the
existing durable before-socket at-most-once fence.

## Images and content delivery

Function/custom outputs and user messages support text plus `input_image` parts.
Only valid single-frame PNG/JPEG/WebP/GIF base64 data URIs are accepted. Exact
source JSON, image bytes, detail hint and tool correlation are preserved. No
remote URL fetch, path substitution, resizing, truncation, or textual stand-in
occurs. Limits are 512 KiB decoded per image, 16 million pixels, eight image
parts in full history, and the existing 1 MiB complete source-request bound.
Global inline context is bounded at 1 MiB; legacy experimental inline remains
256 KiB. Oversize or unsupported images fail explicitly.

The MCP adapter appends genuine ImageContent blocks for images in the delivered
full/delta context. A native controller using `functions.exec` MUST forward each
image block with `image(block)` and forward text separately. Printing only
`text(JSON.stringify(result))` loses native pixel delivery and is not an image
acceptance result. When calling MCP directly, use its image content channel.
The private environment includes pinned Pillow for validation. Metadata reports
mapping/delivery support, not proof that a particular native model saw pixels.

## Evidence levels

- Unit/integration tests: synthetic requests/results, real SQLite ownership,
  schema receipts, localhost HTTP, MCP SDK and bounded image blocks.
- Captured client contracts and E2E: actual official pinned CLI, default cached
  declaration, client tool-search discovery, local namespace MCP invocation and
  typed history echo. The controller responses are synthetic and labelled so.
- Native-hosted acceptance: actual available native web tool, durable preparation,
  complete native result receipt, actual localhost Responses output. HTTP client
  fixture is synthetic; no Mac or Secure MCP Tunnel is involved.
- Native vision acceptance: a native worker rendered an actual ImageContent
  block returned by the MCP adapter and correctly identified unseen fixture
  shapes/colors/text. Its incoming client image was synthetic.

These do not constitute live Mac Desktop/tunnel acceptance or arbitrary Codex
version compatibility. No external model API or production credentials are used.
Run `python direct_bridge/scripts/run_focused_tests.py` from the bundle root with
the declared dependencies installed. Release inventory hashes must be regenerated
and verified separately after review; test success does not publish anything.

## Upgrading an existing local installation

Stop the old shared Direct service explicitly first; this can affect other open
Direct sessions, so finish/reconcile their pending actions before stopping. Keep
the existing state, credentials, profile and database. Put the reviewed package
at its existing approved bundle location and use Start/Setup again.

If the recorded Python is Direct's owned `.venv/bin/python` and lacks the new
pinned dependencies, setup offers a local `UPGRADE` confirmation. It installs
only into that private environment from PyPI, after checking no old service
process remains live. Declining makes no install attempt. Failed installation
leaves the environment for review rather than deleting it. An arbitrary shared
or system Python is never modified; that case gives an explicit dependency
upgrade error and requires the owner to provision the declared requirements for
their chosen interpreter. Status, Stop and Restore never require installing
Pillow or changing credentials.

Model catalogs now use content-addressed immutable filenames. The old
`codex-models.json` is preserved for earlier configuration restore records.
Changed packages/old tool-contract health cannot silently reuse an old service;
session mode asks for explicit Stop rather than stopping other sessions.
Refresh the installed MCP tool definitions and verify all ten global tool names
and the two new input schemas before claiming native-hosted web support.
