Dots2Codex Unified Mac Launcher (offline candidate)
==================================================

First and later runs: double-click START.command
Settings: INSTALL_ROUTER.command or ./START.command settings
Menu: ./START.command menu
Status: ROUTER_STATUS.command
Stop: STOP_ROUTER.command; inspect closed and process_stopped separately

Base published commit: 00156e4621444eefa90752545dc1a40521639fb1
Read docs/UNIFIED_MAC_LAUNCHER.zh-CN.md for setup, permissions and recovery.
Optional Router.app requires a Mac build via mac_router/build-app.command.
The app is unsigned and unnotarized; actual Mac build and launch remain unverified.

Global desktop mode is integrated into the START.command menu. It uses a
separate bounded GLOBAL controller JOIN and one pinned child per desktop thread.
Read docs/DESKTOP_GLOBAL.zh-CN.md. The exact global config diff requires fresh
native preflight evidence and confirmation; production readiness remains false.
Single-session Router actions remain separate. Auth/login/proxy are untouched.

Install only after explicit approval into a private versioned environment.
Reuse only this Mac's existing authorized-user credential, with explicit consent.
No OAuth, scope expansion, credential copying, Google project/folder creation or sharing.
Join message copied only after confirmation; no secret default stdout.
Actual native admission, bidirectional probes and facade readiness are required.

Default 4 hours / 128 model requests; hard maximum 8 hours / 128 requests.
Keep old checkout, runtime, pins and journal. Never rehash an existing runtime.
Live Mac/Google/native end-to-end and multi-hour acceptance remain pending.
