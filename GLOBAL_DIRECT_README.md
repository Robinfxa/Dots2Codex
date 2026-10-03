# Direct：单会话 / 全局，从这里开始

本包以 `af7bac6` 的完整 main 源码为底，提供 Direct 单会话 CLI 和全局 CLI/Desktop 两种启动方式，共用当前多路由后端。使用根目录 **`DIRECT.command`**；其余旧入口及原 main 文档保留作兼容与来源记录，不是本版本的启动步骤。

## 先选范围

- **单会话 CLI**：Start 后选 **1**（默认），只为这次新开的 CLI 传入临时 provider 参数，使用所选 `CODEX_HOME`，不写入、恢复或重整 `config.toml`。要求 `codex-cli 0.159.2`。不修改 sandbox、approval、web_search 等已有安全设置。具体说明见 [单会话模式](direct_bridge/docs/SESSION_MODE.zh-CN.md)
- **全局 CLI/Desktop**：Start 后选 **2**，按 `APPLY` 授权将选定 `CODEX_HOME` 的新会话切换到 Direct。已有窗口不被关闭或重启
- 两种方式共用 MCP、隧道、数据库和原生控制流程。**“单会话”限定本次 CLI 的配置覆盖，不是独立后端**。退出 CLI 后共享服务继续运行，直到你明确 Stop；已有全局配置不会被单会话模式撤销

## 一次设置，以后一键 Start

1. 解压整个目录，保留所有文件。Mac 需要 Python 3.11+、已安装的官方 `tunnel-client`，以及你已有的 **Direct 专用 tunnel**。先正常停止旧 Direct tunnel/bridge。不要绕过 macOS 的安全警告。
2. 双击 `DIRECT.command`，选 **1 Start**，再选单会话或全局。单会话先检查现有 CLI 和项目目录。首次按提示选择现有 tunnel ID 或它的 `dots-direct-*.yaml`，以及要使用的 `CODEX_HOME`。默认状态仍保存在 `~/Library/Application Support/Dots2Codex Direct Global`，兼容现有安装，与源码和旧试验状态分开。
3. 仔细确认首次设置。缺少依赖时，只有输入 `INSTALL` 才会从官方 PyPI 安装到该状态目录的私有环境。运行状态与凭据目录可以分开，优先直接复用已有 Direct 私有目录中的密钥；不会把旧密钥复制进新的运行状态目录。缺失值在本机 Terminal 隐藏输入。只有明确输入 `SAVE` 才保存到提示的私有 env 文件；目录权限为 0700，文件为 0600。隧道 API key 与本地 HTTP bearer 必须不同，不要把它们贴到聊天中。
4. `PROFILE` 确认只为已有 tunnel 创建新的私有本地 profile，不创建远端 tunnel、API key 或权限。单会话无需 `APPLY`，bearer 仅通过 CLI 子进程环境使用。只有全局模式的 `APPLY` 会启用选定 `CODEX_HOME/config.toml` 中的 Direct provider：本地 HTTP bearer 会存入 0600 配置及私有恢复备份，隧道 API key 不会写入 Codex 配置。保存 env 与写入这个本地认证头是两项分别说明的操作。
5. 全局模式启动后，可确认 `OPEN` 打开你选择的已安装 Desktop app。已有窗口请先正常退出再打开，并 **新建会话**。旧会话、其他 `CODEX_HOME`、profile、项目/托管设置或命令行覆盖可能继续使用原 provider；程序不会杀掉你的 Codex 窗口。单会话会直接启动新 CLI，不打开 Desktop。

之后同一设置直接选 **1 Start** 并选择范围。健康且版本匹配的服务会复用；单会话遇到正在运行但不健康或版本不符的服务会停止启动，提示你查 Status 或安排明确的 Stop，不擅自重启。全局模式也不会为更新而暗中重启仍有已记录单会话 CLI 使用的服务。没有 `REOPEN` 试验步骤，也没有全机进程扫描。新建的逻辑路由可选择允许的模型与 effort，已认领路由保持自己的绑定。

## 停止、恢复与更新

- **2 Stop + Restore**：停止本工具记录并确认身份的共享服务进程，同时恢复它改过的全局配置。这会中断单会话和全局客户端的后续请求；发现已记录的活动单会话 CLI 时另需输入 `STOP`。不会关闭 CLI/Desktop 或停止 dot 中的原生任务。仅用过单会话时没有全局配置需要恢复
- **3 Status**：查看本机状态，不启动模型
- **4 Restore only**：单独恢复全局配置；不要求隧道、Google 或密钥。保留无关配置修改；如检测到自己管理的值被另行修改，会停止并提示冲突，不强行覆盖
- **5 Help**：帮助；**6 Open Desktop**：打开选择的已安装 app，不强制关闭现有窗口；**7 Codex CLI**：在选择的项目中启动正常 CLI
- **0 Exit**：关闭菜单，不等同于 Stop
- 更新前先 Stop + Restore，保留私有状态目录。在同一源码目录更新时无需重新创建首次设置；不要为重试删除数据库、未知动作或恢复记录。如把新包放到不同目录，必须先核对并显式重新配置 profile 的启动路径，不能把旧的绝对路径当成新安装已生效

也可运行 `./DIRECT.command session`（单会话）或 `./DIRECT.command global`（全局）。旧 `./DIRECT.command start` 继续表示全局启动，保持脚本兼容。`codex` 仍是普通 CLI，不代表单会话 Direct；`help`、`stop`、`restore`、`status`、`setup`、`desktop` 保留。可选 `--state-dir` 指定私有状态目录。首次/确认流程需要交互式 Terminal。

## 仍需实际原生控制器

本地 Start **不会启动模型、生成答案、自动创建/唤醒原生子任务或设置自动调度**。请在 dot 中明确启动或继续实际原生父控制器，由它按待处理路由创建真实原生子任务，再按精确模型/effort 和认领凭据执行。新模式不使用 Drive/Docs 搬运请求。

MCP 保留八个工具名：`bridge_status`、`get_request`、`discover_tools`、`lookup_schema`、`submit_action_and_wait_result`、`await_result`、`finish_request`、`cancel_request`。**全局参数 schema 已改变**，已有插件必须重新同步，并实际检查工具列表/schema；看到八个旧名字不代表完成升级。状态中的本地 ready 也不证明原生父/子任务已接入。

实际 Mac、Codex Desktop 全局配置、官方 secure tunnel、真实原生父/子任务与多轮工具回传的联合验收仍待完成。离线 HTTP/MCP 与回滚测试不等于生产可用或实际速度证明。兼容元数据来源仍钉在 `codex-cli 0.159.2`。

## 文件完整性与来源

当前整包身份以 `GLOBAL_DIRECT_PACKAGE_MANIFEST.json` 为准；Start/Setup 会校验它。`direct_bridge/MANIFEST.json` 描述本包当前 Direct 子树。根目录 `ROUTER_PACKAGE_MANIFEST.json` 与 `LIGHTWEIGHT_PACKAGE_MANIFEST.json` 保留上游原字节，只描述其历史 main/lightweight 范围，不能代表当前 Global Direct 的验收结果。

详细说明：[真实原生父/子控制器](direct_bridge/docs/GLOBAL_NATIVE_CONTROLLER.zh-CN.md)、[全局配置与恢复](direct_bridge/docs/GLOBAL_CONFIG.md)、[本机凭据](direct_bridge/docs/GLOBAL_CREDENTIALS.zh-CN.md)。

项目 MIT 与 OpenAI Codex Apache-2.0 许可证/通知随包保留。依赖源码、密钥、私有 env、运行数据库和原始日志不在包内。官方隧道说明：<https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>。
