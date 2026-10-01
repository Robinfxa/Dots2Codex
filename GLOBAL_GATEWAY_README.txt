Dots2Codex Global Gateway: offline-integrated preview

This is an EXPERIMENTAL offline prototype, based on published commit 00156e4.
It is not an activated global Codex installation. Production global-config apply
is deliberately disabled. Real user auth/config and the active Router runtime
were not modified. The unified first-launch Mac launcher is now included in the
same source package, but its global menu stays unsupported. A shared package is
not an accepted, seamless global workflow. See docs/EXPERIMENTAL_PREVIEW_VALIDATION.md.

Start reading:
  docs/GLOBAL_GATEWAY.zh-CN.md
  docs/GLOBAL_NATIVE_CONTROLLER.md
  docs/GLOBAL_GATEWAY_VALIDATION.md

Safe offline demo (temporary private fixture files only):
  python3 -m remote_transport.global_fixture

Focused tests:
  python3 -m unittest discover -s remote_tests -p 'test_global_*.py' -v

Implemented:
- Stable private loopback gateway, generation + client/thread isolation
- Exact model/effort pinned before a thread is admitted
- Authenticated bounded Docs CAS admission queue and active-native-controller helper
- Durable one-attempt native admission and unknown-effect/no-replay rules
- Separate existing v3 child JOIN, raw probes, pins, CAS controls, journals and facades
- Bounded first-request wait using parsed Responses JSON heartbeat events
- Same native child/pin/history retained on transport restart
- Explicit global config preview/backup/CAS/restore helper, behind a closed production gate

Still unverified or blocked:
- Real native-controller tool admissions, connected Google and actual Mac desktop/CLI acceptance
- Optional tomlkit installation was not authorized; nine real-parser tests are skipped
- New comments inside owned TOML nodes need real-parser preservation/conflict verification
- One seamless unified Mac launcher + production global configuration workflow

The gateway implementation preserves the source-bound execution contract. The
combined package intentionally includes the unified launcher's wrapper and
preflight/stop changes. No launcher entry point activates this global mode.
Validation did not perform global configuration, real Google operations,
dependency installation, or native inference.
