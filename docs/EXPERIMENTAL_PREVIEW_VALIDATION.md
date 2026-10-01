# Experimental launcher + global gateway preview, 2026-10-01

## Release boundary

This source package combines the unified Mac starter with the offline global gateway prototype.
It is **package coexistence**, not a completed one-click global takeover.
The starter's global menu stays unsupported. The gateway returns
`production_ready=false` and `ready_for_config=false`; production global configuration remains disabled.
There is no supported bypass switch.

Base and rollback reference: `00156e4621444eefa90752545dc1a40521639fb1`.
The launcher input source digest is `7774123803de7f12e89b8cb8be7a510f5cfc2e829572921c0fde33632d4e096e`.
The gateway input source digest is `ab3026308c240f613b8be00b623cfa3eb67cd73fcac4e87556da7d1619215891`.
The current combined allowlist and source digest are in `../ROUTER_PACKAGE_MANIFEST.json`.
Source SHA-256 digests are not Git commit IDs. Historical isolated-candidate counts in
the component validation documents are not the combined count below.

## Full combined regression

All suites ran against the combined source copy, with no new dependency installation:

| Suite | Discovered | Passed | Skipped |
| --- | ---: | ---: | ---: |
| tests | 134 | 134 | 0 |
| remote_tests | 380 | 371 | 9 |
| remote_audit/cas | 47 | 47 | 0 |
| remote_audit/drive | 48 | 48 | 0 |
| sdk_tests | 3 | 3 | 0 |
| docs/gemini_setup/tools | 37 | 37 | 0 |

Total: **640 Python passed, 9 skipped; 25 Node passed**.
The three Google SDK tests reuse already-installed dependencies and mocked network ports.
The offline two-thread gateway demo passed with zero native inference and zero Google writes.
The Node latency benchmark is a synthetic timing check, not production performance evidence.

Coverage includes the six-file materialization contract, complete function-tool continuation
and delivery receipts, thread isolation, fake Google/native admissions, immutable model/effort,
request disconnect/restart/no-replay cases, fixed-port forgery, bounded admission and body deadlines,
config crash reconciliation, launcher preflight, cancel/repeat starts, stop races, partial
environment installation and stale runtime rejection. These are offline fixtures.

## Preservation and publication scope

- All six source-bound files and the frozen POSIX bridge match the published base byte-for-byte
- Gateway production code and tests match their validated candidate; launcher production code,
  scripts and tests retain the validated preflight, lifecycle and stop changes
- Existing PDF/Word guide assets, MIT License, disclaimer and third-party notices are unchanged
- A single regenerated package manifest covers the exact public allowlist; the old isolated
  gateway manifest is intentionally not copied as a misleading combined manifest
- No credentials, auth.json, OAuth tokens, private keys, private runtime, journal, bytecode or
  new execution logs are part of this publication
- Publication does not deploy to a Mac, apply Codex configuration, install dependencies,
  perform OAuth, create/write Google objects, or call native inference

Use a matched new checkout and fresh paired session for any later authorized deployment.
Preserve previous runtime/pins/journals. Never rewrite old source hashes in place.

## Explicit gaps

Nine real TOML-parser tests remain skipped because optional tomlkit 0.13.3 is not installed.
No substitute parser was used. Format-preserving comments, three-way merge and conflict
behavior require genuine parser validation before any future production config gate can open.

Actual Finder/app build, Intel/Apple Silicon, signing/quarantine, real native admissions,
Google access/CAS/probes, terminal and desktop bundled CLI routing, full restarts, resumed
threads, Mac sleep/reboot and long-duration operation still require separate live acceptance.
No automatic native wake or independent backend model attestation is claimed.

## Rollback

The pre-publication reference is the base commit above. If an actual defect requires rollback,
read current main and create a new reverting commit that preserves subsequent changes.
Do not force-reset main or destroy an existing live runtime.
