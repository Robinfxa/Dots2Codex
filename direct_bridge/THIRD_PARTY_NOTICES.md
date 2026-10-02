# Current Global Direct package provenance

The current Global Direct entrypoint is the root `DIRECT.command`. The complete
main snapshot at `af7bac6e95d559f748a4d44777981efa03a6d0f6` is preserved unchanged
in this package. New global modules reuse its MIT-licensed
`dots_lite/config_transaction.py`, `client_catalog.py`, `private_io.py`, and
`protocol.py`; the Direct Responses wire validator derives from
`dots_lite/wire.py` with local support imports and numeric-precision hardening.
The client catalog includes unchanged OpenAI Codex fallback instructions under
the Apache-2.0 license. Root `LICENSE`, `THIRD_PARTY_NOTICES.md`,
`OPENAI_CODEX_LICENSE`, and `OPENAI_CODEX_NOTICE` retain the corresponding notices.

Global mode requires Python 3.11 or newer, `mcp==1.29.0`, `tomlkit==0.13.3`, and
their separately distributed dependencies. Python MCP SDK and tomlkit identify
as MIT-licensed projects. No dependency sources, virtual environment, or tunnel
client executable are vendored in this package. The launcher requests local
approval before optional installation from official PyPI into a private environment.
Dependency licenses remain in their official distributions:
[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.0/LICENSE),
[tomlkit repository](https://github.com/sdispater/tomlkit).

`GLOBAL_DIRECT_PACKAGE_MANIFEST.json` at package root identifies the current
whole bundle. `direct_bridge/MANIFEST.json` identifies the current Direct subtree.
Preserved upstream manifests describe their historical scopes, not current
Global Direct validation. The following original single-route provenance is
retained for its archived sources and measurements; its standalone/no-legacy-import
statement does not apply to the new global modules.

---

# License and source provenance

The Dots2Codex source in this isolated trial is distributed with the unchanged
MIT license and `Copyright (c) 2026 Dots2Codex contributors` notice in LICENSE.
The pure Responses validation/SSE implementation in `facade/wire.py` derives
from the existing Dots2Codex baseline `af7bac6`; its support code is local to this
bundle and does not import or require a legacy checkout.

No Python MCP SDK, tunnel-client, or third-party dependency source is vendored.
The tested Python MCP SDK is `mcp==1.29.0`; its installed distribution identifies
its license as MIT. The SDK and its dependency licenses remain part of their
separate official distributions. See the [official Python MCP SDK repository](https://github.com/modelcontextprotocol/python-sdk)
and [SDK license](https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.0/LICENSE).
The existing test environment also used jsonschema 4.26.0 and anyio 4.14.2,
whose installed distribution metadata identifies their license as MIT.

`context/benchmark-results.json` is an archived synthetic comparison measured
against the published Dots2Codex `af7bac6` RequestView implementation. Its legacy
comparison script and the old module-only manifest are intentionally omitted
from this standalone trial. Reproducing that historical comparison requires
the separately published baseline; it is not a standalone benchmark command in
this ZIP. The direct HTTP + MCP synthetic vertical slice remains self-contained
and can be rerun with `python3 -B -m orchestration.vertical_slice`.
