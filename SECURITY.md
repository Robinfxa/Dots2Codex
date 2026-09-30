# Security policy

Dots2Codex is an experimental, single-user, text-only bridge. Treat deployment directories as private: tickets contain lease capabilities, and queued requests can contain sensitive context.

## Supported boundary

- Keep the HTTP facade on `127.0.0.1`. It is not a public or multi-user authenticated server
- Use a private, locally shared POSIX filesystem with working locks and atomic rename
- Keep the official CLI's sandbox, approval prompts and platform access restrictions in force
- Use a fresh deployment and isolated CLI configuration. Never copy authentication files or publish runtime directories
- Treat a missing heartbeat or disconnected environment as uncertainty, not permission to duplicate an inference task
- Do not use this project to bypass account access, payment, quotas, service restrictions or security controls

Same-user processes with write access to the deployment directory are inside the trust boundary. Hashes and epochs detect stale or inconsistent state; they are not a defense against a malicious same-user process able to rewrite the directory.

There is no 24/7 monitoring or automatic security-update guarantee. Review changes and validate the exact environment before relying on them.

## Reporting a vulnerability

If this repository offers private vulnerability reporting, use that channel. Otherwise, open an issue asking for a private reporting route without posting exploitation details or private data. Do not include credentials, request bodies, tickets, user transcripts, CLI home directories or raw runtime archives in a public issue.

For a reproducible report, prefer a small synthetic test, affected revision, expected behavior and observed error code. Public GitHub issues are not a secure credential channel.
