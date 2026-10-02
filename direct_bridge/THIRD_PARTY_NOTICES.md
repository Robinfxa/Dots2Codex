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
