# Desktop app discovery and explicit configuration trials

## Why no internal executable picker

CC Switch resolves the Codex configuration directory and manages provider/model settings. Its current source uses an optional CLI/cache/static-template sequence for model-catalog templates; an internal desktop engine path is not a mandatory part of its normal provider-switch workflow. Dots2Codex previously combined configuration setup with a much stricter two-engine evidence gate and guessed one bundle location. When that guess failed, it asked the user to find an app-internal executable. That requirement is removed from the normal Global flow.

Global startup now discovers signed `com.openai.codex` application bundles using LaunchServices, an exact bundle-ID Spotlight query, `/Applications`, and `~/Applications` candidates. It does not recursively scan the user's home, inspect credentials, launch the app, or execute an internal desktop binary. An app renamed or moved from `/Applications/Codex.app` can be discovered through registered metadata. Two valid installations produce a list of app locations and display versions, not an internal file picker. An unregistered custom installation can be supplied with advanced `--desktop-app /path/to/Installed.app`.

Before accepting an app identity, the code validates Info.plist, its bundle ID and declared main executable, rejects symlinked or unsafe evidence files, and asks Apple's codesign tool to verify the bundle and the pinned OpenAI signing identity. It records and rechecks the Info.plist, main executable and signed resource map hashes/stat identities. The current identity pin is `com.openai.codex`, OpenAI team `2DC432GLL2`. A changed identity/signature requires review; it does not trigger a permissive fallback. App display versions are recorded exactly and never translated into a CLI engine version. Missing metadata or unverifiable signatures fail before a configuration write.

## Two separate evidence profiles

- `strict-client-binaries/1`: the existing explicit `--desktop-codex` path verifies both actual engine binaries as `codex-cli 0.159.2`. Existing strict evidence cannot be downgraded by omitting the desktop version or attaching app metadata
- `desktop-app-config-trial/1`: the normal flow verifies the terminal adapter's exact CLI separately, plus the signed desktop app identity. It deliberately does not claim the app's engine or model-catalog compatibility. The app may have a different internal engine or no exposed standalone engine at all

Both profiles still bind the package, parser, activation, current native controller, signed queue, uniquely admitted child, completed native request, random challenge, and proof expiry. A socket alone, caller-supplied app version, or `ready` boolean cannot authorize writing. The trial profile cannot satisfy the strict profile API. Changing app identity, signature resources, parser, terminal binary, or package invalidates its evidence before commit.

The terminal adapter remains pinned to 0.159.2 because this project's generated model catalog and wire behavior were derived from that source version. This is not a requirement imposed by Codex provider configuration in general. Removing that independent restriction requires separate catalog/protocol compatibility work.

## What the user confirms and verifies

The exact-diff dialog identifies the detected app/version and target `CODEX_HOME/config.toml`. It explicitly labels the operation a reversible compatibility trial and states that the native backend preflight does not test the desktop app. No write occurs until the user confirms this dialog. Only the existing owned configuration keys change; `auth.json`, other credentials, safety policy, and unrelated settings remain untouched.

After applying, fully quit and reopen the desktop app, then create a new thread and send a simple request. Check Global status for a completed new client route. Old/resumed threads, profiles, CLI overrides, managed settings, and another `CODEX_HOME` may keep using other settings. If the app rejects the version-pinned catalog, no request reaches this gateway, or the trial is unwanted, use **Restore Global config**.

Status reports `completed_client_routes` / `client_route_observed` only for completed requests on new, non-preflight routes created after the transaction committed, while that exact configuration is still present and the same activation is active. An unknown delivery, an old route, a preflight route, edited/restored configuration, or expired activation cannot produce a positive trial observation. The receipt is generic client traffic: HTTP headers are not desktop-process attestation. `desktop_new_thread_verified`, `desktop_compatibility_verified`, and `production_ready` remain false. A person checking the actual desktop new thread and receipt is still required for desktop acceptance.

Restore uses the same backup, compare-before-write and conflict handling as before. A failed/unknown trial never installs a different app, changes authentication, rewrites history, or silently falls back to an official provider.

## Primary evidence reviewed on 2026-10-01

- [CC Switch current source, pinned 1bc68e293f635a065fa984d4c2ca7604fbd77854](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/src-tauri/src/codex_config.rs), particularly config-directory resolution and `load_codex_model_catalog_template_uncached`
- [CC Switch's own Codex configuration guide](https://github.com/farion1231/cc-switch/blob/1bc68e293f635a065fa984d4c2ca7604fbd77854/docs/guides/codex-official-auth-preservation-guide-zh.md). Its auth modes differ from this project; no CC Switch authentication behavior is copied here
- [OpenAI advanced configuration: custom model providers and CODEX_HOME](https://developers.openai.com/codex/config-advanced/). This documents configuration, not a stable internal desktop executable path or universal catalog compatibility
- [Apple NSWorkspace application lookup](https://developer.apple.com/documentation/appkit/nsworkspace/urlforapplication(withbundleidentifier:))
- [Apple code-signing requirements and verification](https://developer.apple.com/library/archive/technotes/tn2206/_index.html)
- Upstream app users' observed bundle identity/layout/signing team: [openai/codex #31068](https://github.com/openai/codex/issues/31068), [#19041](https://github.com/openai/codex/issues/19041). These are observed installation reports, not OpenAI compatibility guarantees. The implementation checks actual signed metadata instead of treating their path examples as mandatory

## Validation limits

All automated app fixtures are synthetic; Apple's signature/metadata commands are stubbed in Linux tests. Tests cover discovery, moved/renamed/custom apps, missing internal engines, missing or unknown metadata, wrong bundle identity, signature failure and updates, symlinks, non-macOS, profile separation, commit rechecks, diff cancellation, exact restore, and honest post-apply route status. No actual Mac, app signature, native inference, Google resource mutation, or user configuration write was exercised in this change. Real TOML parser tests remain dependency-based skips if `tomlkit 0.13.3` is absent. These limitations must not be described as a live acceptance pass.
