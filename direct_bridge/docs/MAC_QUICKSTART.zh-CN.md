> **已归档：下文仅供旧单路 CLI 试验参考，不是当前全局版的操作指南。**
> 当前请从 [Direct 全局版指南](../../GLOBAL_DIRECT_README.md) 开始，使用根目录 `DIRECT.command` 的 Start / Stop / Restore；要求 Python 3.11+。
> 下文旧的试验目录、试验专用 CLI 启动参数、`REOPEN`、双 Terminal 环境变量及“不改全局配置/不保存凭据”等描述，只属于保留的旧脚本。不要用于全局版。
> 原生控制端请使用 [当前全局参数和真实子任务流程](GLOBAL_NATIVE_CONTROLLER.zh-CN.md)；MCP 名称相同不代表旧 schema 可直接复用。

# Mac Direct 试运行：从旧版切换到新分支

入口是新分支仓库根目录的 **`DIRECT.command`**。第一次需完成本地设置和官方隧道授权；之后仍要启动隧道、一个原生子会话和一个新的 Codex 会话。脚本不能替你登录、生成凭据、授予插件权限，或保证当前 dot 的子会话已支持这条 MCP 连接。

本指南只做单会话 CLI 试运行，不改 `~/.codex/config.toml`、旧 profile 或桌面全局配置。真实 Mac / 原生模型 / 隧道的端到端验收尚未完成。

## 1. 先停旧版，再取独立 worktree

仍在原来的 `main` 目录，用**运行当前 v3 服务的同一版本**先执行下面两条命令，停止 Lightweight 服务并恢复它管理的配置：

```sh
./LIGHTWEIGHT.command stop
./LIGHTWEIGHT.command restore
```

在 dot 中确认这一轮旧原生任务已结束。停止本地进程不等于远端任务已结束；存在恢复冲突或结果未知时先处理，不要覆盖配置。只有你另外使用过旧版 Global 模式，才再用对应旧版本的 `START.command` 做 Global stop / Restore Global config；它不是当前 v3 的默认停止入口。

完成停止和恢复后，保留原目录与未提交文件，从原仓库目录执行：

```sh
git status --short
git fetch origin feat/direct-mcp-trial:refs/remotes/origin/feat/direct-mcp-trial
git worktree add --detach ../Dots2Codex-direct origin/feat/direct-mcp-trial
cd ../Dots2Codex-direct
./DIRECT.command check
./DIRECT.command setup
```

若目标目录已存在，换一个全新的兄弟目录，不要清空它。这里使用独立的 detached worktree，不切换或覆盖原来的 `main`。新分支只使用 `DIRECT.command`；旧入口及旧清单留作历史，可能因分支新增文件而拒绝运行，不能重做旧清单来绕过校验。要用旧版时回原来的 `main` 目录。

## 2. 按 setup 提示准备这一次试运行

需要 Mac 上已有 Python 3.10+、固定 `codex-cli 0.159.2`、官方 `tunnel-client`。`check` 只检查，未安装项会明确列出。Python、Codex 和隧道客户端不由脚本静默安装。

`setup` 优先复用已符合版本的 Python 环境；缺依赖时，单独询问是否建立专用 venv 并安装固定 MCP 依赖，再让你确认一个新的本地 route。默认状态放在 `~/Library/Application Support/Dots2Codex Direct`，其中 `route/config.json` / `route/launcher.json` 是非秘密配置，`route/state.sqlite3` 是首次运行时建立的独立数据库；可用 `--state-dir /绝对/新目录` 隔离另一轮试运行。核对模型、推理档位、端口和授权时间窗，记下输出的**完整 stdio 命令**。已有状态不会自动重置。

这是一个 route 对应一个逻辑 Mac 会话、一个逻辑原生子会话。默认模型 `gpt-6-astra` / `xhigh` 需要实际宿主能够接纳；这些配置字符串本身不能证明模型身份或可用额度。

## 3. 完成官方隧道的首次连接

先准备你已获准使用的专用 tunnel ID 和 runtime key。这个 tunnel 必须关联**当前 dot 所在 ChatGPT workspace**；调用者需要所属 Platform organization 的 Tunnels Read + Use，开发者模式权限另算。没有这些权限时，先在官方页面完成授权，不能靠本地脚本解决。[官方隧道指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

在 Mac Terminal 中由你本人提供两个不同的现有值：

- `CONTROL_PLANE_API_KEY`：只供官方隧道客户端登录控制面
- `DOTS_BRIDGE_HTTP_BEARER`：专用于本机 Codex 与 bridge 的 HTTP 验证；不能用上面的 runtime key 或账号登录 token 代替

使用你已有的安全环境变量注入方式。也可在你自己的终端进入 `/bin/bash` 后隐藏输入；**不要把值写进命令、profile、JSON、聊天或日志**：

```sh
/bin/bash
read -r -s -p '现有本地 HTTP bearer: ' DOTS_BRIDGE_HTTP_BEARER; printf '\n'
export DOTS_BRIDGE_HTTP_BEARER
read -r -s -p '现有 Tunnel runtime key: ' CONTROL_PLANE_API_KEY; printf '\n'
export CONTROL_PLANE_API_KEY
```

隐藏输入不会显示或写入 shell 命令历史，但变量仍属于进程环境，不是凭据保险库。若你还没有获准使用的本地 bearer / runtime key，先停在这里，由你完成凭据准备；启动器不生成或保存它们。

首次创建**一个新的专用本地 profile**时，优先审阅并执行 `setup` 输出的完整 `tunnel-client init` 模板，只替换 tunnel ID。模板已带本轮唯一的 `dots-direct-…` 名称和正确的路径引用。也可按下面的等价步骤手工输入：将 tunnel ID 占位值替换为真实 ID；下一行提示时粘贴 `setup` 输出的完整 stdio 命令，以保留路径中的空格和引号；不要加入秘密值，也不要使用 `--force` 覆盖旧 profile：

```sh
tunnel-client help quickstart
IFS= read -r -p '本轮唯一 profile 名称: ' DIRECT_PROFILE
IFS= read -r -p '粘贴 setup 输出的完整 stdio 命令: ' DIRECT_STDIO_COMMAND
tunnel-client init --sample sample_mcp_stdio_local \
  --profile "$DIRECT_PROFILE" \
  --tunnel-id YOUR_EXISTING_TUNNEL_ID \
  --mcp-command "$DIRECT_STDIO_COMMAND"
tunnel-client doctor --profile "$DIRECT_PROFILE" --explain
./DIRECT.command run
```

`run` 输入并选择这个专用 profile，并请你核对它指向当前 worktree、当前私有 route 和端口。保持终端前台运行。不要再手工启动第二份 `start_mcp.sh`。日后已有正确 profile 时，无需再次 `init`。

官方客户端 v0.0.15 源码的 stdio 子进程继承运行环境；bridge 启动时移除它不需要的控制面 key。本地 bearer 则供 loopback 验证使用。profile 默认保存 `env:CONTROL_PLANE_API_KEY` 引用，命令文本可能被客户端显示或记录，因此其中绝不能含凭据。实际安装版本仍需用 `doctor` 和真实工具调用确认。[实现依据](MCP_AND_TUNNEL.md#official-references-and-source-checks)

## 4. 把连接加入实际 dot，并检查同一个子会话

按照官方连接页，在目标 workspace 的 Plugins 中创建开发者模式连接，Connection 选 Tunnel，选上面的 tunnel，查看发现的八个 bridge 工具；再将连接启用到实际使用的 dot。这个账号/工作区连接步骤由你确认，`DIRECT.command` 不会自动完成。[官方连接步骤](https://developers.openai.com/plugins/deploy/connect-chatgpt)

让 dot 先调用 `bridge_status`，再创建**一个**与你确认的模型/档位匹配的原生子会话。子会话必须独立看到并调用同一连接的 `bridge_status`，返回相同 route。任何一边缺工具、没有额度或模型接纳失败，就停在此处，不要先向 Mac 发工作请求。八个工具名字或 schema 出现在文字里不算接通。

可把 [最短控制器规程](NATIVE_CONTROLLER.zh-CN.md) 发给 dot；它描述的是真实 MCP 工具调用流程，不需要粘贴凭据。

## 5. 第二个终端，只启动一个新的 Codex CLI 会话

第二个 Terminal 进入同一 worktree。由你以步骤 3 的隐藏输入方式提供**相同的本地 bearer**；这个终端不需要隧道 runtime key。然后运行：

```sh
./DIRECT.command codex
```

启动器核验 `codex-cli 0.159.2`，让你选择一个已有项目的绝对路径并确认 `CODEX`，根据当前 route 使用本轮唯一的 `dots_direct_<route 摘要>` provider、HTTP/SSE、`env_key`、关闭 WebSockets 和 hosted web search，以 `--no-daemon` 启动新的单会话。所有 provider 选择通过本次进程参数传入，不写入全局配置。首次只读验收保留 Mac sandbox 与正常审批；不要加绕过权限的参数。

先发一句“只回复 DIRECT_READY，不执行工具”。确认结果来自已接通的同一个原生子会话，再做三次依赖前一输出的无害只读 Mac 调用。详细验收见 [live acceptance](../orchestration/README.md)。启动器保存一次性会话准入标记，即使 CLI 之后退出也不会让同一路由再开新会话。不要删除标记来重试，也不要在同一路由再开第二个 Codex、新线程、resume 或 fork；数据库里逻辑 session ID 也不是平台 thread ID 的加密证明。

需要手工核对参数时看 [CLI 参数与桌面边界](CODEX_CLIENT.zh-CN.md)。旧会话不会迁移到新 route；桌面全局接管不属于这条一键路径。

## 6. 结束、异常和下一次

正常结束先退出 Codex 会话，并让 dot 结束该原生子会话，再在隧道终端 Ctrl-C。关闭终端后环境变量随 shell 退出；若保留 shell，可主动 `unset DOTS_BRIDGE_HTTP_BEARER CONTROL_PLANE_API_KEY`。

- `status` 只报告本地配置/进程信息，真实可用性以双方 `bridge_status` 和实际回调为准
- 没有 `/models`、浏览器首页或健康检查 HTTP API；不要用浏览器打开端口来判断 bridge ready。业务接口只接受带 bearer 的 `POST /v1/responses`（也支持 `/responses`）
- `pending`、504 或断线表示结果可能未知；保持原 request/action ID，用 `await_result` 观察。不要重新发提示词、换子会话、删数据库或重复执行副作用
- 重启后遇到 `execution_admission_unknown_no_replay` 必须先核对上一轮真实执行结果。旧 route 过期也不能直接改时间窗续跑
- 下一次独立试运行先结束并核对上一轮，使用新的状态目录和新的专用 profile / route；不要覆盖前一轮证据

## 验证范围

已建立真实 loopback HTTP + MCP stdio 的合成回调测试；它不调用外部模型、不执行 Mac 工具。Codex 0.159.2 的版本/参数帮助、固定版本源码和官方配置文档已核对，隔离配置的 `features list` 也成功加载了所列 provider 参数；本次云端实际 CLI 请求探测在客户端自身 sandbox 初始化阶段被阻止，尚未到 HTTP，因此不计为 CLI 端到端通过。没有为此改安全设置。

未验证：Mac 安装/启动、真实 Secure MCP Tunnel、实际 dot 和原生子会话工具可见性、真实模型接纳、Mac 工具三轮闭环、桌面 App 和真实延迟。这些都要按上述步骤在你的 Mac 上完成。
