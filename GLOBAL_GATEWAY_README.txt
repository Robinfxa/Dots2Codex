Dots2Codex Desktop Global integration

Double-click START.command and choose Global start.
Read docs/DESKTOP_GLOBAL.zh-CN.md for the actual desktop activation flow.

Global mode starts its dedicated stable-port supervisor, creates one bounded
signed Google control queue after consent, and supplies the private
DOTS2CODEX_GLOBAL_JOIN_V1 message. This activates a native controller, which must
remain active and create a distinct selected child for each new desktop thread.
It is different from the ordinary single-worker JOIN.

A native nonce preflight must complete through the same gateway before the UI
can show and confirm a global config diff. The pilot gate rechecks controller,
activation/catalog, route/pin, native admission, request/response, exact binaries,
installed TOML parser, package hashes, and proof expiry at replacement time.
Production readiness stays false. A listening socket or boolean cannot enable it.

Fully quit and reopen Codex after apply, then create a new thread. Existing or
resumed threads may keep their previous provider. Profiles, CLI overrides and
managed settings can take precedence. Use the actual shared CODEX_HOME.

Global status/stop/restore are available without dependency installation.
Restore recovers config transactions even after an interrupted success marker.
Local stop never claims that the bounded native controller/children stopped.

Offline validation uses temporary homes, native-shaped fake ports and real local
gateways/facades. No real user config/auth, Google resources, dependency install,
native inference or push occurred during this integration. Real Mac/Google/
native acceptance remains pending. tomlkit is absent; nine real-parser tests
remain explicit skips until its installation is authorized.
