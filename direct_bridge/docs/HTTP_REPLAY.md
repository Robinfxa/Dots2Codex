# Bounded HTTP replay for Direct

Only the Direct-owned session and global provider tables set
`request_max_retries = 1` and `stream_max_retries = 0`. The pinned CLI is
`codex-cli 0.159.2`. Other providers and client safety settings are unchanged.
Desktop engines of other versions require separate verification.

## What is retried

The pinned client's HTTP-opening layer prepares the request once, then can
clone those same bytes and headers for one additional attempt after a 5xx
response or an eligible opening transport failure. This is at most two HTTP
attempts for that prepared request. It does not retry 401 or 409 responses.
Once a successful HTTP response has opened its event stream, stream retries
remain disabled; a broken stream terminates instead of rebuilding the prompt.

The bridge recognizes an existing request using its original `session-id`,
`thread-id`, complete canonical request body, and idempotency key when one was
provided. Whitespace and object-key order do not matter; changed request
contents do. Re-entering the same user text is a new client operation and is
not an exact transport replay. No new native worker, request sequence, action
ID, or execution authorization is created by an exact replay.

The legacy `automatic_retry: false` error field remains a conservative
no-general-replay hint. The pinned SDK does not interpret that JSON field;
its separately configured, single byte-identical HTTP-opening retry is the
explicit bounded exception. Neither that exception nor the error response
authorizes native/action re-execution, a changed payload or identity, or a
stream retry. `resubmit_new_action: false` remains unchanged.

## Safety and delivery limits

- A timeout before commitment leaves the same request pending. A late text
  answer, or the first emission of a client-tool intent, can reach the second
  HTTP attempt without another native commitment.
- Text-only results may be replayed without recommitting them.
- Client-tool responses retain the durable one-use emission fence, saved
  before socket output. A replay after that fence returns 409
  `delivery_outcome_unknown_no_reemission`, even if the first socket failed.
  Nothing resets that fence or repeats the native action.
- A dropped first connection can leave its server waiter alive. If it wins
  the emission race, the live retry may receive 409 although no tool reached
  the client. This conservative unknown outcome is intentional. A retry
  improves delivery opportunities; it does not guarantee recovery.
- Two pending 504 responses exhaust the retry budget. A subsequent worker
  commitment does not create a third HTTP attempt or wake the finished CLI
  turn. The unchanged database retains the result for controlled inspection.
- `http_emissions` records first emission reservation, not client receipt.
  `finish_request` returning `http_delivery_confirmed: false` does not by
  itself mean that delivery failed; this interface has no client receipt.

The published default `http_wait_ms` is 300,000 ms. Two full HTTP wait windows
therefore total approximately ten minutes per logical inference request, plus
connection and retry-backoff overhead; this is not a total-turn completion limit. Actual configured values and transport interruption may shorten
that time. The SSE idle timeout starts after response headers arrive; raising
it does not extend the bridge's pre-header HTTP wait deadline.

This setting does not fix or explain intermittent upstream MCP cancellation.
It does not automatically resume a stopped native controller. CLI 0.159.2 has
no `/retry` command or generic Retry control after its turn has failed.
`/resume` restores a saved conversation and does not guarantee an identical
original HTTP request. Changing this setting cannot revive an already-ended
failed turn. Do not suggest a new prompt, new route, deleted state, or a new
action ID as recovery for an unresolved action.

## Verification scope

Isolated tests used the actual pinned CLI and unmodified production bridge
with synthetic native responses. They verified exact body/header replay,
late text and one harmless `get_goal` roundtrip, two-timeout exhaustion,
pre-header and post-header drops, terminal 401/409 responses, and durable
owner/context/tool fences across restart. The harmless client-tool result was
observed once. No external tools, live model API, user Mac, or production
bridge state participated. These tests do not establish a live tunnel SLA.

`tests/test_http_replay_recovery.py` provides portable bridge regressions for
timeout/restart replay, one-use tool emission, bounded pending requests, and
request conflicts. Provider-generation tests assert the one/zero retry
budgets. The pinned upstream implementation prepares once and clones for
HTTP retries in
[EndpointSession](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/codex-api/src/endpoint/session.rs#L133-L148).
