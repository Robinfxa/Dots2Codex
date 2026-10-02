# Fixture provenance

All values, identifiers, content, URLs and revision strings in these fixtures are synthetic. No Google provider, Mac, native inference service, or publication endpoint was invoked to make them.

The normalized output field topology is transcribed independently from the baseline's sanitized, previously captured connector-schema regression in `remote_tests/test_global_indexed_queue.py::test_normalized_full_wrapper_accepts_proven_nullable_metadata` and `test_normalized_response_nullable_target_is_accepted_without_mutation`. It is not produced by the parser under test. The text, fragments and all UTF-16 indexes were independently set for this fixture. The exact text is `開始😀\n漢字と🚀𝄞\nend\n`; its UTF-16 length is 17 and its indexed replacement must delete `[1,17)`, retaining the final mandatory newline.

`connector_tool_contracts.json` is exact read-only discovery of installed tool descriptions and input schemas on 2026-10-02. Input schema discovery is not output-schema or live transport acceptance. Raw SDK nested-tab compatibility is tested separately.

`normalized-raw-fetch.json` and `normalized-file-metadata.json` preserve the field topology and null/empty values of earlier saved actual connector responses (`global-child-690759/forward-fetch.json` and `forward-metadata.json`). Every nonempty identifier, URL, title, owner detail, date, size and text summary was replaced with synthetic data without publishing the originals. The actual raw-fetch response carries empty `content` plus `b64_string:null`, and an additional nested `structuredContent`; this is a real compatibility requirement, not a synthetic minimal port assumption. The read-only derivation made no live provider call.

Golden schema tests catch real normalized nullable-field regressions; synthetic provider fault tests exercise state transitions. Neither test class proves live Google latency, native scheduler behavior, the actual selected model internals, Mac integration, or successful client tool execution.
