# Guides + optional setup helper migration validation (2026-10-01 UTC)

## What was combined

This candidate starts with the repaired Router public tree
`8da5c5ada6c3fa85dc64317653f5f7961b8adf4eed73ae9a44563e8141a4e5c8`.
All 144 repaired-base public paths remain. Only the new guide/helper paths from
the supplied Gemini guide archive were imported; its older Router code was not
copied over the repaired implementation. The top-level README gains guide links,
and the release manifest is regenerated. No baseline runtime or dependency file
is changed by this migration. See [the repaired Router validation](ROUTER_VALIDATION.md)
and [upgrade checklist](ROUTER_UPGRADE.zh-CN.md) for its existing fixes and limits.

The archive contains one complete checkout, including Markdown, HTML, Word, PDF,
the optional helper, and the helper's offline tests. Use the sibling validation
summary for the final archive hash and this checkout's `ROUTER_PACKAGE_MANIFEST.json`
for its exact public-file inventory. The manifest excludes itself from the
source digest, which is SHA-256 of the canonical sorted path-to-file-SHA256 JSON
mapping (`sort_keys=True`, separators `(',', ':')`).

## Scoped helper repairs

- Use the actual `InstalledAppFlow.run_local_server` parameter
  `authorization_prompt_message`; the message contains no `{url}`. Authorization
  and callback URLs, state, codes and token contents are not emitted in helper
  status/error output. SDK logging is suppressed only during the operation
- Reject every `web` block, including dual `installed`/`web` configs. Validate the
  Desktop client's fields and exact Google endpoints before invoking the SDK;
  pass only the normalized `installed` block. Explicitly enable PKCE
- Bind loopback to `127.0.0.1` on a random port with a 300-second consent wait.
  Refuse existing output and preserve files created while the browser is open.
  Require a refresh token, accepted scope evidence when supplied by the SDK,
  and valid serialized credential fields before saving a private file
- Require owner-private regular files and an existing owner-private parent
  directory; reject final symlinks and refuse credential overwrite
- Folder creation reserves a durable private receipt first, then uses one raw
  HTTPS POST. Redirect following, SDK socket replay, authorization replay and
  application/status retries are absent. Validate returned folder metadata.
  A lost reply or receipt-save failure stays unknown and cannot be rerun with
  the same receipt. Reconcile locally instead of choosing a new receipt to retry
- The helper remains opt-in and is never invoked by Router installation/startup.
  It does not change sharing, discover other credentials, or start inference

## Reproducible offline checks

From the extracted checkout:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s remote_tests -v
python3 -B -m unittest discover -s remote_audit/cas -v
python3 -B -m unittest discover -s remote_audit/drive -v
python3 -B -m unittest discover -s sdk_tests -v
python3 -B -m unittest discover -s docs/gemini_setup/tools -v
node --test native_connector/runner.test.js native_connector/test_adapter.js
node native_connector/benchmark.js
```

The Google SDK checks need the optional packages. This migration used already
available dependency directories; no dependency installation was performed.
The public helper tests need only Python's standard library and mock all OAuth,
browser and network effects. Fresh-extraction results are recorded separately
in the final validation summary. They cover 441 base Python tests, 37 helper tests
and 25 Node tests, plus syntax, local-link and archive-integrity checks.
Independent offline review separately covers 26 unique repaired-Router cases
and 17 helper cases. The helper's actual-SDK mocked control-flow exercise used
`google-auth-oauthlib 1.5.0`; compatibility with the preserved pinned `1.2.2` API
was checked against its [official source](https://raw.githubusercontent.com/googleapis/google-auth-library-python-oauthlib/v1.2.2/google_auth_oauthlib/flow.py),
not by claiming that pinned package was executed. The requirements files pin
selected direct packages; they are not a complete transitive lockfile.

## Not performed or established

No real OAuth consent, installation, real Google create/read/write, Mac launcher
execution, live native-worker admission, active-runtime replacement or repository
publication was performed for this migration. Offline mocks do not establish
actual grants, account access, cloud-console UI availability, cross-application
Drive access or live latency. Existing synthetic parallel benchmark values are
scheduling comparisons only. Real Mac/Google pairing, bidirectional raw-file
probes, stop/recovery behavior and multi-hour endurance still need explicit,
operator-approved live acceptance. An existing historical long-session run does
not validate these new setup and Router paths.
