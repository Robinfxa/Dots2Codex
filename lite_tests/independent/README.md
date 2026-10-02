# Independent offline audit

This suite was developed outside the implementation checkout and integrated only after independent findings were fixed and integration was approved. It exercises the real lightweight protocol, worker, gateway, generated adapter cells and Python helpers against independently implemented synthetic ports. No production Google or native model operation occurs. One test uses a real temporary loopback HTTP listener; no Mac, installed user configuration or publication is touched.

Run once via the normal recursive suite:

    python3 -B -m unittest discover -s lite_tests -p 'test_*.py' -v
    node --test native_connector/lite*.test.js

The config tests require the project's pinned real `tomlkit`, not a mocked parser. `LITE_REPO` may override the checkout path; no absolute workspace path is required. Do not additionally run an external copy when counting unique tests.

Coverage: normalized and raw SDK Docs shapes, nullable optional revision fields, independently fixed UTF-16 indexes, exact CAS packet retention, missing/stale/malformed acknowledgement classification, durable once-only admission and exposure, interrupted native-boundary counter, missing journal refusal, owner handoff, immutable result retries, route-local uncertainty, exact function/custom tool IDs and namespaces, full-history isolation, two arbitrary project/thread routes, duplicate request IDs, restart delivery fences, real loopback SSE, 100-turn bounded storage, and stopped-old/owned-config migration.

Fixture provenance is in `fixtures/PROVENANCE.md`. All fixture values are synthetic. Real normalized output field topology is preserved, including `body:null`, `nestingLevel:null`, `targetRevisionId:null`, and raw-fetch `content:""`/`b64_string:null`.

The separate `native_connector/lite_independent_roundtrips.test.js` ledger measures both the first request after fresh admission and a second full-history request in that same child. It records in-cell helpers, static source loading and out-of-cell emission separately. Model input reading, actual native output generation/file writing, real native spawn/handoff scheduling, live Google latency and Mac latency are not measured. These tests establish offline correctness and operation counts, not live acceptance or model attestation.
