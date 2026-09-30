# License scope and source provenance

The top-level MIT license applies to the original Python code, tests, schemas and documentation authored for Dots2Codex. It does not relicense external software, services, names or trademarks.

## What `vendor/` contains

`vendor/` is a frozen snapshot of **this project's own earlier Python bridge and launcher**. It is not a copy of the OpenAI Codex implementation or CC Switch. These project-authored files are covered by the repository's MIT license:

- `vendor/core.py`, `launcher.py`, `service_adapter.py`, `readiness.py`, `broker_wait.py`
- `vendor/bridge_core/file_queue.py`, `facade.py`, `broker.py`
- `vendor/bridge_core/reference/bridge.py`, the original project-authored Responses/file-queue prototype

The name `reference/bridge.py` refers to that earlier local implementation. Its SHA-256 is pinned by the project loader to detect accidental changes. No upstream Rust source, Codex executable, bundled model prompt, CC Switch implementation or MCP SDK is distributed here.

## External projects consulted, not redistributed

- [OpenAI Codex](https://github.com/openai/codex), especially tags `rust-v0.159.2` and `rust-v0.159.0-alpha.7`: consulted for provider configuration, request headers and Responses event compatibility. Codex has its own [Apache-2.0 license](https://github.com/openai/codex/blob/rust-v0.159.2/LICENSE). Users obtain the official executable separately; this repository does not modify or ship it.
- [CC Switch](https://github.com/farion1231/cc-switch): consulted for configuration preservation, error handling and protocol design ideas. Its [MIT license](https://github.com/farion1231/cc-switch/blob/main/LICENSE) remains its own; no CC Switch code or assets are included.
- [multi-subflow](https://github.com/Robinfxa/multi-subflow): its public README was a conceptual reference for file handshakes. No scripts or other implementation were copied.
- [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12/json-schema-core): the repository's original schema documents declare this dialect. The specification and its implementation are not bundled.

## Separately installed software

The text-only runtime uses Python's standard library. Python and the user's official Codex installation retain their respective licenses and terms. The optional `tool_probe` and `repo_review` runtime scopes, and the complete test suite, require separately installed [`jsonschema`](https://github.com/python-jsonschema/jsonschema) 4.x to validate advertised schemas. The package is not vendored or automatically installed; it retains its own license. See `requirements-test.txt` and the dependency preflight in README.md.

Protocol field names, URLs and compatibility references do not imply affiliation, endorsement or permission to access a service. If future changes copy third-party implementation code or assets, preserve their notices and review their licensing before redistributing them.
