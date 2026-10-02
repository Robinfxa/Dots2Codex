# Foreground MCP bridge and Mac setup

Start with the [Chinese Mac quickstart](MAC_QUICKSTART.zh-CN.md) and repository-root
`DIRECT.command`. This page documents the lower-level stdio/runtime contract.

## What is runnable now

`mcp_adapter` is an actual stdio MCP server backed by the existing official Python MCP SDK. It starts the local Responses facade in the same process. This is a single-owner experimental bridge: it makes no inference API calls, invents no tool results, and executes no Mac commands. Native reasoning and Mac tool execution must happen in their respective real clients.

The build workspace has Python 3.12 and `mcp==1.29.0` already installed. The SDK requires Python 3.10 or newer. `requirements-mcp.txt` records the tested SDK version. The low-level stdio launcher does not install dependencies. The separate guided
`DIRECT.command setup` can create a private venv and install these dependencies
only after its explicit INSTALL confirmation. A later Mac needs a Python environment containing those dependencies before the startup command can run; no Mac installation or tunnel configuration was performed in this build.

### Process boundaries

- The Mac client posts full Responses requests to `http://127.0.0.1:18765/v1/responses`, authenticated with a separate local bearer supplied at runtime
- `tunnel-client` launches `scripts/start_mcp.sh` as its stdio command
- The admitted native controller calls the static MCP tools through its configured tunnel connection
- The bridge validates, queues, and returns exact tool intent; only the Mac client executes that intent and submits actual outputs in its next request
- EOF, Ctrl-C, or SIGTERM stops the foreground process; nothing is registered for automatic background startup

## Trust contract

This prototype trusts a dedicated, single-owner stdio connection and the user's approved tunnel/workspace access. The controller assigns one logical child identity and an immutable grant/route/session/thread/model/effort binding. Those fields are locally configured and never accepted as tool arguments. `_meta`, names supplied by a model, and caller-asserted identity strings cannot change that binding.

The configured worker ID is a logical application identity, not cryptographic proof of native platform identity, a model, or a physical machine. This does not supply multiuser authorization or attest that a parent and child have identical plugin availability. Confirm the actual calling client's tool availability during the live smoke test. Anyone who can call this dedicated connection is inside its configured controller trust boundary; do not share it broadly.

The approval reference records the trusted controller's explicit user authorization. A JSON string is not authorization by itself. Runtime setup requires a bounded authorization window. Repeated delivery can reread the same claim; a separate durable one-use execution reservation and action/request fingerprints prevent turning a lost response into a fresh execution. A timeout or broken connection is an unknown outcome, never permission to replay a side effect with a new identifier.

Context/schema tokens acknowledge what the bridge returned and what the caller echoed. They do not prove that a model read or retained content. Restart recovery is fail-closed; do not erase durable state, reset identifiers, or start a replacement worker to bypass an unresolved action.

## Start locally after explicit user setup

1. Copy `mcp_adapter/config.example.json` to a private local route configuration. Replace every `REPLACE_...` value. Choose the approved model and reasoning effort. Set explicit Unix timestamps for `not_before` and `expires_at`. The example intentionally cannot start unchanged.
2. Use an existing, separately authorized local HTTP bearer in the `DOTS_BRIDGE_HTTP_BEARER` process environment. Do not put a bearer, an OpenAI API key, or any credential in the JSON configuration. The launcher never generates, saves, or prints one. Do not paste credentials into chat or shell command history.
3. Run the foreground command from the bundle, using an absolute path for the configuration:

```sh
./scripts/start_mcp.sh --config /absolute/path/route.json --http-port 18765
```

`PYTHON_BIN` may select an already-prepared Python interpreter. The SQLite path must have an existing parent directory. Relative database paths resolve beside the configuration file. Stdout is reserved for newline-delimited MCP JSON. Without an MCP client, the process simply waits on stdin; do not type arbitrary commands there.

Do not run a second copy when the tunnel owns the same stdio process. Port conflicts fail closed. The local Responses bearer is separate from the tunnel control-plane key. The child removes `CONTROL_PLANE_API_KEY` from its environment because the MCP process does not need that tunnel credential.

## User-driven Secure MCP Tunnel setup

The [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) supports a private stdio command. Use an existing tunnel ID and an authorized runtime key; running the client requires organization-level Tunnels Read + Use. The tunnel must be associated with the intended workspace, and developer-mode permission is separate. This bundle does not create/edit a tunnel or configure persistent access.

The guided setup prints a complete, shell-quoted stdio command with its selected
Python interpreter and absolute route path. Use that exact command in the dedicated
profile; do not substitute the system Python or put secrets in the command.

On the Mac, inspect an already installed client with `./scripts/tunnel_help.sh`; that script only runs `tunnel-client help quickstart`. If the binary is missing, consult the official guide and its current vendor download link. No automatic download or installation is included.

After the user authorizes creating the named local profile, run the guide's stdio setup with real local paths and an existing tunnel ID. `CONTROL_PLANE_API_KEY` must be securely provided in that process's runtime environment. The following placeholders are not working credentials or identifiers:

```sh
tunnel-client init --sample sample_mcp_stdio_local \
  --profile USER_CHOSEN_PROFILE \
  --tunnel-id EXISTING_USER_TUNNEL_ID \
  --mcp-command '/absolute/bundle/scripts/start_mcp.sh --config /absolute/path/route.json --http-port 18765'

tunnel-client doctor --profile USER_CHOSEN_PROFILE --explain
tunnel-client run --profile USER_CHOSEN_PROFILE
```

`init` writes a local profile and is deliberately not part of the launcher. Use
a new dedicated name and never `--force` over an existing profile. The official
v0.0.15 template uses `env:CONTROL_PLANE_API_KEY` rather than storing a literal key. `run` must stay active for discovery and calls. Have the user select the existing tunnel in the intended connection surface,
enable it for the actual dot, and verify tool visibility independently in its
real native child. Successful discovery in an unrelated chat is insufficient. No extra public HTTP MCP endpoint is needed. Keep the tunnel client's admin interface loopback-only. Inspect readiness before testing from the real native controller.

## MCP surface and protocol

`tools/list` is static and contains eight small tools; full Mac schemas are fetched only as needed:

| Tool | Purpose |
| --- | --- |
| `bridge_status` | Report configured logical route and local readiness |
| `get_request` | Claim/reread the next bound context delivery |
| `discover_tools` | Resolve request-bound tool keys without loading all schemas |
| `lookup_schema` | Retrieve one exact schema and its delivery token |
| `submit_action_and_wait_result` | Commit one native action and wait for actual next-request output |
| `await_result` | Observe an existing action without emitting it again |
| `finish_request` | Commit the final native response |
| `cancel_request` | Cooperatively cancel without claiming rollback |

Every schema rejects extra top-level arguments. The SDK manages initialization, version negotiation, pings, tool calls, JSON-RPC responses and cancellation. The tested SDK accepts the 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25 protocol versions; unknown versions negotiate its latest supported 2025-11-25. This bridge advertises only tools, no task extension, sampling, prompts, roots, or 2026 protocol features.

The original SDK stdio reader has no line-size bound, so thin binary-file wrappers supply bounded UTF-8 lines to the unmodified transport. Input is capped at 2 MiB per line; invalid UTF-8, oversized lines and unterminated EOF close the transport. Tool results are capped at 1.5 MiB, encoded output messages at 4 MiB, active runtime calls at eight, and MCP call execution at ten seconds. Explicit wait arguments allow 0–5000 milliseconds. The timeout is cooperative with the runtime and does not roll back a committed action.

All stdout lines are MCP messages. Dependency logging is disabled because SDK validation diagnostics can contain entire requests. Stderr receives only fixed event labels. Tool exceptions return fixed error codes, never raw exception text. Troubleshooting should use request/action state rather than enabling payload logs.

Protocol references: [official SDK](https://github.com/modelcontextprotocol/python-sdk), [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [initialization and version negotiation](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle), and [cancellation](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation).

## Verification and remaining live acceptance

From the bundle root:

```sh
python -m unittest discover -s tests -p 'test_mcp_stdio.py' -v
python -m unittest discover -s tests -p 'test_mcp_config.py' -v
```

The pipe fixture is explicitly synthetic and exists only under `tests/`. It verifies real OS pipes, SDK initialization/version negotiation, the tool inventory, malformed input, identity-override rejection, concurrency, cancellation, timeouts, byte limits, and redacted errors. Fixture success is not evidence of Mac tool execution or a live tunnel.

A real-client smoke test must still establish:

1. The existing tunnel is connected in the intended workspace and its stdio command stays healthy
2. The actual native child can list and call the bridge's tools with the configured model/effort and one continuing context
3. The Mac client's Responses request reaches the loopback facade with the authorized local bearer
4. A harmless, explicitly authorized Mac tool call executes once, and its real output appears in the next request
5. Repeated delivery, transport interruption, cancellation, and process restart never cause a second side effect

Do not claim live readiness, native platform attestation, or Mac execution from this cloud-only build. Keep the route narrowly scoped while performing those acceptance checks.

## Official references and source checks

Checked 2026-10-02; these are source/documentation checks, not a live tunnel test.

- [Official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels): current stdio init/doctor/run workflow, organization-level permissions, and workspace association. Use its current vendor download link instead of a hard-coded release asset
- [Official plugin connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt): developer-mode Tunnel connection and actual tool discovery
- [Official v0.0.15 stdio child implementation](https://github.com/openai/tunnel-client/blob/v0.0.15/pkg/mcpclient/stdio_command.go): creates the configured argv with Go `exec.Command`, without overriding `cmd.Env`. Normal Go subprocess behavior therefore inherits the runtime environment; it is not an implicit shell. Its command text is operator-visible, so credentials must never appear there
- [Official profile template](https://github.com/openai/tunnel-client/blob/v0.0.15/cmd/client/profile_samples/sample_mcp_stdio_local.yaml.tmpl) and [init implementation](https://github.com/openai/tunnel-client/blob/v0.0.15/cmd/client/init_command.go): one dedicated main stdio channel and a default environment secret reference; replacement requires an explicit force option that this guide does not use
- [Codex client settings and test boundary](CODEX_CLIENT.zh-CN.md): `env_key`, HTTP/SSE, no WebSockets, pinned CLI and session-local flags

The bridge only implements authenticated `POST /responses` and `POST /v1/responses`.
There is no HTTP model catalog, compact endpoint, health endpoint, or WebSocket
transport. Check bridge readiness through the actual `bridge_status` MCP tool;
check tunnel readiness through the official client's own health/admin surfaces.
