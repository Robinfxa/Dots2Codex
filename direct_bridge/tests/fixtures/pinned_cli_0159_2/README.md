# Official Codex 0.159.2 captured contracts

These are sanitized excerpts of real outgoing requests from the official pinned CLI. Tool declarations and post-response callback items are exact, not reconstructed. Initial instructions, environment and user messages were replaced with a fixed test message. No HTTP headers or credentials were captured.

The server/controller answers and local MCP data were synthetic. Passing these fixtures is not a claim of native-model inference or live-Mac acceptance. The custom apply_patch result intentionally records rejection under the retained read-only sandbox. The reasoning callback fixture remains an explicit unsupported-input boundary.

Each file includes original request/catalog file hashes, exact component hashes, binary hash, source commit, and sanitization details. manifest.json binds all fixture file bytes. The pinned source is https://github.com/openai/codex/tree/ff6aec96948b70d94983af2641a6b67c94faeff5.

Regenerate from an isolated raw capture directory using:

    python3 extract.py /path/to/direct-real-contract-capture

Run the regression tests from the repository root:

    python3 direct_bridge/tests/test_pinned_client_contract.py
