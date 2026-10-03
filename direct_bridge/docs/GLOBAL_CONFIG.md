# Global Codex configuration contract

This transaction contract applies only to `DIRECT.command global` / legacy
`start`, or the menu's explicit global choice. `DIRECT.command session` uses
per-process CLI overrides and never invokes preview/apply/reconcile/restore;
see [session mode](SESSION_MODE.zh-CN.md). Existing global transactions remain
unchanged when a session CLI reuses the shared service.

`direct_bridge/global_config.py` is the Direct adapter to the main release's
`dots_lite/config_transaction.py` and `dots_lite/client_catalog.py`. It imports
main's format-preserving TOML parser, snapshots, symlink and ownership checks,
cooperative home lock, atomic writer, exact-byte and syntax-aware three-way
restore, and reviewed catalog metadata. It does not modify those main modules.

## Scope and consent

After the launcher verifies its own local authenticated HTTP service and MCP /
tunnel readiness, the user reviews the exact Codex home and accepts the global
configuration change. Readiness is checked again immediately before commit.
The callback must return `True` or `{ "ready": true }`; a missing/false result is
not readiness. A local listener is not evidence of a successful native model
roundtrip.

The transaction owns only these top-level fields and its one provider table:

- `model_provider = "dots2codex_direct_global"`
- `model`, initially `gpt-6-astra`
- `model_reasoning_effort`, initially `xhigh`
- `model_catalog_json`, an immutable private catalog path
- `[model_providers.dots2codex_direct_global]`

Other providers, comments, profile/project configuration, safety settings and
`web_search` are preserved. The provider uses Responses over loopback HTTP with
WebSockets and automatic retries disabled. Hosted search, Responses Lite and
in-place effort updates are not supported. Existing enabled search is not
silently disabled; a request using an unsupported capability can fail closed.

Both ordinary CLI launches and Finder-launched Desktop can load the selected
home's config. This is conditional on both clients actually using that home.
Profiles, project config, command-line flags and managed settings may override
it. Fully quit/reopen each client and use a new thread; existing threads may
retain their prior selection. The launcher does not kill unrelated clients or
global daemons.

## Local authentication, not Terminal environment propagation

Desktop does not generally inherit an interactive Terminal's environment. The
provider therefore uses an explicitly approved static HTTP header:

```toml
[model_providers.dots2codex_direct_global.http_headers]
Authorization = "Bearer <bounded-local-bridge-token>"
```

The actual value is generated/reused by the launcher's credential module. It is
only the local HTTP bridge bearer. The control-plane/tunnel API key is never
passed to this module or written into the Codex configuration. The base URL is
restricted to `http://127.0.0.1:PORT/v1`, without query parameters or redirects.
`requires_openai_auth = false`, no `env_key`, and no OpenAI account token are
used for this provider.

First-use consent must explicitly name the config file and disclose local-token
persistence there and in private transaction postimages. If an existing config
is not mode `0600`, tightening its permissions also needs explicit approval.
The write atomically creates/replaces mode-`0600` config. Journals and before/
after images are mode `0600` inside a mode-`0700` transaction directory. Preview,
status and journal JSON redact or omit the bearer and prior config values.
They are suitable for ordinary status display; raw before/after files are not.

This behavior is grounded in the pinned official CLI source, **0.159.2**, commit
`ff6aec96948b70d94983af2641a6b67c94faeff5`:

- [`http_headers` configuration and header construction](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/model-provider-info/src/lib.rs#L168-L173)
- [Static headers are installed without an environment dependency](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/model-provider-info/src/lib.rs#L390-L417)
- [Custom-provider auth selection does not forward ambient account auth when `requires_openai_auth` is false](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/model-provider/src/auth.rs#L197-L221)

The actual installed Desktop engine version is unknown and has not been tested
by this source-only change. No Desktop compatibility or paid/native admission
is inferred from CLI source or local fixture tests.

## Restore and interrupted operations

Each successful apply keeps its original snapshot and one durable transaction.
Repeated Start with the same config and owner reuses that transaction instead
of replacing the user's restore point. A different state directory cannot
silently take ownership of an existing Direct provider. An active transaction
with changed owned fields requires review.

Restore does not need a running server, tunnel, API credentials or MCP SDK.
Exact-byte restore does not need `tomlkit`; a three-way merge does require the
pinned `tomlkit==0.13.3`. Unchanged files restore exactly, including comments and
newlines. Unrelated later edits are retained by main's three-way restore.
Changes to an owned value or its syntax/comment stop restore without overwriting
them. Restore retains mode `0600` rather than loosening privacy. It never deletes
auth files or model catalogs and never terminates clients.

Journals are `prepared`, `committed`, `restore_prepared`, `restored` or `aborted`.
Reconciliation can prove whether a pre/post-write crash committed, restore an
aborted attempt to a retryable state, or leave an uncertain/conflicting result
for review. No failed restore is silently marked successful. Original backups
remain available after restore; do not publish or attach those private files.

## Fixture verification

`tests/test_global_config.py` uses isolated temporary homes and a real pinned
TOML parser. It covers original/unrelated settings, search preservation,
permissions and redaction, idempotent and cross-owner Start, stale previews,
symlink/origin rejection, pre/post apply and restore crashes, owned conflicts,
exact restore without parser, CRLF, and immutable multi-pair catalogs. All keys,
paths and tokens in those tests are synthetic. The tests do not contact external
APIs, change the user's Mac, or prove native model admission.
