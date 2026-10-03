> **已归档：下文仅供旧单路 CLI 试验参考，不是当前全局版的操作指南。**
> 当前请从 [Direct 全局版指南](../GLOBAL_DIRECT_README.md) 开始，使用根目录 `DIRECT.command` 的 Start / Stop / Restore；要求 Python 3.11+。
> 下文旧的试验目录、试验专用 CLI 启动参数、`REOPEN`、双 Terminal 环境变量及“不改全局配置/不保存凭据”等描述，只属于保留的旧脚本。不要用于全局版。
> 原生控制端请使用 [当前全局参数和真实子任务流程](docs/GLOBAL_NATIVE_CONTROLLER.zh-CN.md)；MCP 名称相同不代表旧 schema 可直接复用。

# Dots2Codex direct bridge: single-owner trial

A small, runnable local bridge for a real native child to drive Mac Codex through
static MCP tools. The warm path returns each verified Mac callback as a native
tool result. It removes Drive/Docs from that path, keeps one child per conversation,
and exposes only new context after the initial bootstrap.

This is an experimental protocol bridge, not a production acceptance claim.
The local Codex session selects it as a custom provider; the bridge itself does no inference. Python transports and validates data; it does not call an external model
API, substitute a model, execute Mac tools, or fabricate product responses.

## What is verified

The bundled executable fixture passes **three sequential synthetic callbacks plus
final response over actual loopback HTTP and an actual MCP stdio pipe**. It verifies:

- One configured logical actor, one full context bootstrap, then three deltas
- Exact call/output correlation, immutable action IDs, one exact schema lookup
- Duplicate and timeout observation without a new tool emission
- Cancellation before commitment and explicit too-late cancellation afterward
- Fail-closed acquisition after competing-process/restart ambiguity
- Changed history, schema, authorization and malformed HTTP rejection

The fixture's model outputs and Mac outputs are explicitly synthetic. Actual
native child tool visibility, model admission, Secure MCP Tunnel operation, and
real Mac command execution remain unverified. Local fixture milliseconds are not
an estimate or promise of end-to-end model latency.

## Mac start here

Use **`DIRECT.command` at the repository root** and follow the
[Chinese Mac quickstart](docs/MAC_QUICKSTART.zh-CN.md). It covers stopping/restoring
an old `main` installation, a separate `feat/direct-mcp-trial` worktree, reviewed
local setup, an existing official tunnel profile, actual dot/child tool visibility,
and one new Codex CLI session. The old root launchers remain historical; run them
only from their matching `main` checkout, never by bypassing their manifests.

The guided entry point can prepare a private Python environment after an explicit
install confirmation and commit one new non-secret, time-bounded route after
review. It does not create credentials, initialize tunnel profiles, change global
Codex settings, or connect a plugin to a dot. First-use authentication and
workspace authorization remain user steps. Finder may not inherit the required
runtime environment; use Terminal for the credential-dependent run steps.

## Run the offline checks

Use an already prepared Python 3.10+ environment with the dependencies in
`requirements-mcp.txt`. The low-level `start_mcp.sh` never installs packages;
the separate guided `DIRECT.command setup` asks before a private dependency install.
From this directory:

    python3 -m unittest transport.test_queue context.test_incremental facade.test_runtime orchestration.test_vertical_slice -v
    python3 -m unittest discover -s tests -p 'test_mcp*.py' -v
    python3 -m orchestration.vertical_slice

The last command prints its evidence as JSON. It starts only a temporary local
fixture process, closes it afterward, and performs no native inference, Mac
execution, deployment, account mutation, or persistent access setup.

## Later, on the authorized Mac

Start with [the Mac quickstart](docs/MAC_QUICKSTART.zh-CN.md), then
[the foreground MCP and Secure MCP Tunnel guide](docs/MCP_AND_TUNNEL.md).
The low-level tunnel-owned process is `scripts/start_mcp.sh`; its configuration example deliberately
contains unusable placeholders. Use a private new route and an existing approved
runtime bearer supplied through the process environment. The local Responses
listener binds only `127.0.0.1`; the dedicated supported tunnel owns the MCP stdio
process. No public cloud server is required.

Before a live trial, confirm the configured static tools (ten in global mode) can actually be called by
both the parent and one admitted native child. Tool schemas returned as data do
not install tools. WebSockets or MCP notifications do not establish automatic
native-model wakeups. The supported host connection and real live test decide
whether this architecture is available.

Read the [minimal native controller](docs/NATIVE_CONTROLLER.zh-CN.md),
[session-local Codex configuration](docs/CODEX_CLIENT.zh-CN.md),
and the [minimal native/Mac acceptance experiment](orchestration/README.md),
[facade contract and failure behavior](facade/README.md),
[transport rules](transport/README.md), and [context rules](context/README.md).

## Trust and limitations

- Dedicated single-owner stdio/tunnel/workspace access is trusted. The configured
  worker ID is logical ownership, not cryptographic native identity attestation.
  Do not expose this service as a public or shared multi-user endpoint.
- Mac tools retain the actual Codex sandbox and approval requirements. The bridge
  neither executes those commands nor expands their permissions.
- Full source history stays local to the Mac bridge and is validated before delta
  projection. No semantic history is silently dropped. Large bootstrap/tool
  results can exceed the actual native host's output/context limit: that must be
  measured live. The projection fails explicitly at its own bounded inline limit.
- Timeouts and disconnected streams leave outcomes pending/unknown. Reuse the
  exact action ID. Do not reset the state DB, replace the actor, or re-emit an
  uncertain tool intent. Restart recovery is deliberately fail-closed.
- The supported wire subset is text and advertised function/custom tools. Other
  content forms, hosted tools, and arbitrary Codex versions are not established.
- No tunnel/account setup, credential generation, package installation, Mac
  configuration change, deployment or repository push was performed by the build.

Historical comparison data under `context/benchmark-results.json` (when included)
compares the old projection with the new one using synthetic source data. It is
not a live latency measurement. Its old-baseline reproduction requires the
separate published `af7bac6` source checkout; the standalone runtime and vertical
slice do not require that checkout.

## Source and license

This trial is derived from Dots2Codex. `facade/wire.py` preserves the baseline
`af7bac6` request/response/SSE validator, with the support import adapted and
lossless inner tool-argument numeric checks added. It imports no Drive controller.
The existing [MIT license](LICENSE), copyright Dots2Codex contributors, is copied
unchanged. No bundled Mac binary, credential, or external model client is included.

## Tool contract v2

See [pinned-client tool compatibility](docs/TOOL_COMPATIBILITY.md) for client tool discovery, native-hosted web receipts, bounded native image blocks, precise unsupported options, and evidence boundaries. Global MCP now exposes ten tools; existing eight contracts are retained.

## 本机脱敏诊断

运行 `./DIRECT.command diagnostics --lines 80` 查看有限的时间线与脱敏关联 ID。只读，不重启、不读取密钥或请求内容；旧版本没有历史事件可还原。保留上限、字段含义与排障边界见 [脱敏诊断说明](docs/DIAGNOSTICS.md)。
