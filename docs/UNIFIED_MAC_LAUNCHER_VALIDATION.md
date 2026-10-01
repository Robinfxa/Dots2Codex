# Unified Mac launcher validation

Candidate base: published commit `00156e4621444eefa90752545dc1a40521639fb1`.
This is offline validation, not deployment or real Mac acceptance.

## Covered

- Shared preflight before config/workspace mutations; exact folder ID/MIME/not-trashed checks
- Exact credential candidate paths, explicit reuse, recorded scope rejection, unsafe files/symlinks
- Native dialog/TTY adapter, cancellation, false list, timeout, no-TTY fail-closed, safe argv quoting
- Explicit private venv creation/install consent; pinned direct versions, health imports and pip check
- Unique final venv paths, atomic pointer only, incomplete install preservation, locks, repeat reuse
- Paired model/effort storage; active/stale blocking; no duplicate Docs or runtime replacement
- Clipboard explicit consent/failure, private output, configuration CAS conflict
- Router readiness requires owned process, matching binding, nonclosed live loopback status
- Original Router CAS/stop, source-hash binding and model-selection regression suite
- Shell syntax and applet template inspection

The completed test counts and clean-archive digest are recorded in the release result outside the package.
The source manifest hashes every public file except itself.

## Not run / not claimed

- macOS osacompile, Finder double-click, Terminal focus, AppleScript GUI, Gatekeeper/notarization
- Intel/Apple Silicon, real Python/Codex discovery or actual dependency installation
- Real Google credentials/OAuth, cloud writes, bidirectional probes or native inference
- Global Codex routing, desktop routing or simultaneous CLI threads
- Multi-hour endurance, fresh-account native entitlements, independent backend model attestation

No installed tools, auth, Google objects, user global Codex config or live sessions were changed.
The optional app build is unsigned and requires separate real Mac acceptance.
