# Config-first local Codex takeover and advanced app trials

## Normal flow: select the configuration, not an app

The normal Global path uses `client-config-trial/1`. It resolves one exact safe `CODEX_HOME` from explicit selection, saved state, the `CODEX_HOME` environment variable or the standard `~/.codex` location. Known conflicting locations require a choice; the final consent dialog always identifies the actual `CODEX_HOME/config.toml`. Existing ownership, permissions and path-safety checks still apply. This does not scan other accounts or create an alternate home to bypass a failure.

There is no normal-flow app installation, application-name, bundle-ID, code-signature or app-internal executable gate. A local consumer may read the chosen configuration without providing any of those app details. This does not establish that a running consumer uses that home, accepts this project's catalog, or has reloaded it. In particular, this is not a claim that all ChatGPT Work surfaces, cloud tasks or desktop clients are supported.

The exact official `codex-cli 0.159.2` catalog-adapter evidence remains mandatory. This project's generated catalog and wire behavior were derived from that version; removing app discovery does not remove the adapter compatibility gate. That is a Dots2Codex constraint, not a general requirement for switching a Codex provider. Supporting another adapter requires separate catalog/protocol compatibility work.

## Three separate evidence profiles

- `client-config-trial/1`: the default, config-first trial. It binds the exact CLI catalog adapter and safe selected home without desktop app evidence. Consumer and desktop compatibility remain unverified
- `desktop-app-config-trial/1`: only when explicitly requested with `--desktop-app /path/to/Installed.app`. It retains the signed-app identity checks and separately pinned terminal adapter, without claiming the app's internal engine or catalog compatibility
- `strict-client-binaries/1`: only when explicitly requested with `--desktop-codex /path/to/codex`. Both actual selected engine binaries must report `codex-cli 0.159.2`. App display metadata cannot replace a binary version observation

These profiles are separate. Missing advanced-profile evidence cannot silently become a default config-first trial, and a default trial cannot satisfy the strict API. Explicit signed-app checks retain their previous identity/signature and evidence-recheck behavior; the normal flow does not invoke them.

All profiles still require fresh sealed evidence binding the package, installed real parser, catalog adapter, activation, current native controller, signed queue, uniquely admitted child, completed native request, random challenge and proof expiry. The config-first profile additionally binds the canonical config target and its safe directory identity. Restore and reconciliation require that original directory identity too; a replacement directory at the same pathname cannot receive the backup. Missing or unsafe targets leave status non-positive and require revalidation. Before committing the reviewed config transaction, the gate rechecks its evidence and configuration before/after hashes. A socket alone, a caller-supplied version, or a `ready` boolean cannot authorize writing. The normal production readiness gate stays closed.

## What the user confirms and verifies

The dialog shows the exact `CODEX_HOME/config.toml` target and redacted exact diff. It labels the change a reversible compatibility trial and explains that successful native backend preflight does not test the actual consumer. No write occurs until the user explicitly confirms. Only existing owned configuration keys change; `auth.json`, other credentials, safety policy and unrelated settings remain untouched.

After applying, fully quit and reopen the consumer that should read that home. Include its app-server or managed daemon where applicable: closing a window or sending another request is not proof that a startup-loaded catalog was refreshed. Start a fresh thread, send a simple request, and check Global status for a new completed post-commit route. A newly launched terminal CLI must also use the same selected `CODEX_HOME` if terminal coverage is claimed.

Old/resumed threads, profiles, project settings, CLI overrides, managed settings and another `CODEX_HOME` may keep using other providers. This workflow does not rewrite history or resolve those overrides. If the consumer rejects the version-pinned catalog, no request reaches this gateway, or the trial is unwanted, use **Restore Global config**, then fully restart the affected consumer and any applicable app-server/daemon again.

Status reports `completed_client_routes` / `client_route_observed` only for completed requests on new, non-preflight routes created after the transaction committed, while that exact configuration is still present and the same activation is active. Unknown delivery, old or preflight routes, edited/restored configuration and expired activations cannot yield a positive trial observation. This is generic traffic evidence, not proof of which app process sent it or attestation of the underlying model. `desktop_new_thread_verified`, `desktop_compatibility_verified` and `production_ready` remain false. Observing the actual restarted consumer alongside its new route is still a live acceptance step.

Restore retains the private original-byte backup, compare-before-write, interrupted-transaction reconciliation and conflict handling. Unrelated edits are preserved; conflicting owned edits stop restoration rather than being overwritten. A failed/unknown trial does not install an app, change authentication, rewrite history or silently fall back to an official provider.

## CC Switch reference and design boundary

The reference inspected on 2026-10-01 is pinned to CC Switch commit `1bc68e293f635a065fa984d4c2ca7604fbd77854`; these links are a reproducible source snapshot, not a claim that the commit will remain current.

- [`codex_direct.rs`, direct-provider execution](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/src-tauri/src/services/provider/codex_direct.rs#L1077-L1132): the direct switch proceeds through prepare → plan → run, constructing file patches and catalog work without an app-installation identity gate
- [`codex_config.rs`, configuration directory](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/src-tauri/src/codex_config.rs#L273-L279): use the configured override or `~/.codex`. Its override comes from CC Switch settings, not Dots2Codex's `CODEX_HOME` selection logic
- [`codex_config.rs`, catalog template loading](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/src-tauri/src/codex_config.rs#L2033-L2046): cache, optional CLI bundled-model export, then static-template fallback. Dots2Codex retains its own exact-version adapter requirement
- [`codex_client_catalog.rs`, startup catalog diagnostics](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/src-tauri/src/services/provider/codex_client_catalog.rs#L1-L16): a retained app-server catalog can differ from per-request configuration. Its [embedded `ChatGPT.app` app-server fixtures](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/src-tauri/src/services/provider/codex_client_catalog.rs#L700-L729) support stale-client diagnostics, not a required app brand or universal support claim

CC Switch is design inspiration for a configuration-first workflow. No CC Switch code or authentication behavior is borrowed in this change, and no license change is implied. Dots2Codex preserves its own native-preflight gate, privacy boundaries, transaction safety and evidence profiles.

## Optional signed-app checks retained

The explicit `--desktop-app` profile checks Info.plist, the bundle ID and declared main executable, rejects symlinked/unsafe evidence files, and asks Apple's codesign tool to verify the bundle and pinned OpenAI signing identity. The existing pin is `com.openai.codex`, OpenAI team `2DC432GLL2`. Info.plist, main executable and signed resource-map hashes/stat identities are recorded and rechecked. App display versions are recorded exactly and never translated into a CLI engine version. Missing metadata, changed identity or unverifiable signatures fail closed; they do not select the normal profile as a fallback.

These checks are optional advanced diagnostics/evidence, not proof that the app actually consumed the selected configuration. Their existence does not introduce an app gate into normal startup.

## Validation limits

Automated app fixtures are synthetic; Apple's signature/metadata commands are stubbed in Linux tests. Existing offline coverage for advanced discovery and trial paths is not live Mac evidence. Current profile and transaction tests must be reported separately from historical test counts; no unchanged suite is implicitly re-certified by this document.

Real Mac/consumer and native acceptance remain pending. All nine actual `tomlkit 0.13.3` integration tests remain explicitly skipped in the current environment; dependency absence is not a pass and no parser installation was performed. This change did not install software, log in, alter user authentication/configuration, mutate Google resources, run native inference or push to Git. A source package and successful offline tests do not activate the user's machine.
