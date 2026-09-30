# Security policy

Dots2Codex is an experimental, single-user bridge with text-only defaults. Treat deployment directories as private: tickets contain lease capabilities, and queued requests can contain sensitive context.

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

## Sticky-routing extension

`routing.py` adds bounded multiple logical sessions for the same owner, not a
multi-tenant service. Use fresh dedicated native contexts; never reuse a native
task ID for another session/generation. Routing credentials and markers do not
create platform authority. Only the actual authorized parent can admit a task.
The deterministic guard is active only inside the current registry lock/thread,
and stale/expired generations cannot renew, read or complete.

Do not call lower-level FileQueue APIs directly for routed worker activity.
Same-user write access can bypass cooperative fencing; protecting against a
malicious same-UID process is outside this prototype's supported boundary.
Unknown inference, delivery and tool outcomes require actual evidence. Resolving
an inference retires its original job and does not prove a tool never ran.

## Optional tool scopes

Enabling `tool_probe` admits only one immutable nonce command. Enabling
`repo_review` admits only `repo_fetch.py` with the exact checked arguments and
public repository destination. Both require `jsonschema` validation of the
currently advertised tool schema and fail closed when validation is unavailable.
The broker produces an intent; only the official desktop CLI executes it.

Repository text is untrusted data. It cannot authorize a new command, destination,
credential, permission, or change in scope. Repository review pins the commit,
checks paths against the returned tree, reserves every intent, and pins correlated
output hashes. An ambiguous or running execution remains unresolved and is never
automatically replayed. A terminal fetch failure permits only a factual final.

The helper uses anonymous GET, rejects redirects, disables proxy-environment use,
and has strict time/body/output limits. It does not provide general network access.
The observed live attempt returned `network_unavailable`; the exact underlying
DNS/proxy/sandbox cause was not established. Do not relax permissions or switch
execution paths to bypass an access restriction.
