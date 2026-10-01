# Dots2Codex

**连接官方 Codex CLI 与已活跃、获授权的原生推理 worker 的实验性桥接工具。**

保留本地 POSIX 共享目录桥接，同时提供远程 Google Drive + Docs CAS 传输、
有界长会话、Mac 工具续轮，以及单消息 Mac Router 配对。Python 负责传输、状态和回执；
模型推理仍由平台实际接纳的原生 worker 完成。

[快速开始](#快速开始) · [工作原理](#工作原理) · [验证状态](#验证状态) ·
[文档导航](#文档导航) · [安全与排障](#安全与排障) · [免责声明](DISCLAIMER.md)

> 本项目是独立社区实验，不是 OpenAI 或 Google 的官方产品，也不代表其认可。
> 不提供模型、免费额度、权限绕过、自动唤醒或无限运行保证。使用前请阅读
> [免责声明](DISCLAIMER.md)与[安全边界](SECURITY.md)。

## 快速开始

### 1. 选择入口

| 你的场景 | 从这里开始 |
| --- | --- |
| 已有经批准的 Mac OAuth 文件、专用 Drive folder 和双向访问条件 | [已有环境的 Mac Router 操作手册](docs/gemini_setup/03_MAC_ROUTER_RUNBOOK.zh-CN.md) |
| 第一次配置 Google Cloud / OAuth / Gemini 辅助设置 | [Google Cloud 与 Gemini 指南](START_HERE_GEMINI_GUIDES.md) |
| 需要手动配置远程传输或排查 CAS | [远程安装配置](docs/REMOTE_SETUP.zh-CN.md) |
| 两端已有私有 POSIX 共享目录 | [本地桥接安装与运行](LOCAL_BRIDGE_README.zh-CN.md) |

已有环境优先复用本人批准的本机凭据和 folder。不要重复建项目、扩大权限，
也不要把另一端的 OAuth 文件复制到 Mac。

### 2. 获取同一版本的完整仓库

```sh
git clone https://github.com/Robinfxa/Dots2Codex.git
cd Dots2Codex
git rev-parse HEAD
```

记录并人工核对 commit，两端固定到**同一个 commit**。不要只复制一个 Python 文件，
不要用旧 OneClick ZIP 覆盖新版目录，也不要覆盖正在运行的 worker。
升级须使用新会话、私有 runtime 和新 pin，详见[升级与现场验收](docs/ROUTER_UPGRADE.zh-CN.md)。

基础要求：Python 3.11+、POSIX 环境。Mac Router 当前要求官方 `codex-cli 0.159.2`；
版本不匹配会停止，其他版本需独立验收。[官方 Codex CLI](https://github.com/openai/codex)
须自行安装，本仓库不打包 CLI、模型或凭据。

### 3. Mac Router：安装一次，每次会话发送一条私有 join 消息

先审阅[安装与边界](docs/ROUTER_ONE_CLICK.zh-CN.md)，并确保本人已批准需要的安装与云资源操作。

1. 运行 `INSTALL_ROUTER.command`：在项目内创建独立 `.venv-router`，询问已批准的 folder ID、
   Mac 自己的 authorized-user 文件路径及 Codex 工作目录。安装器不登录、不扩权、不复制 token
2. 运行 `START_ROUTER.command`：Mac 新建彼此独立的 bootstrap Doc 和正式 Control Doc，
   准备 forward probe，并显示一条私有 join 消息
3. 将整条消息发给获授权的 Dots，随后清空剪贴板。Dots 必须先获得平台真实 native admission，
   按[同版本配对规程](docs/ROUTER_JOIN_V1.zh-CN.md)通过私有文件读取 join-code，
   核验签名、双向 raw-file probe 和正式 Control CAS
4. Mac 校验签名 polling ack 后启动 loopback facade。看到 `ROUTER_READY` 后进入 Codex；
   查询状态用 `ROUTER_STATUS.command`，结束会话用 `STOP_ROUTER.command`

**“一键”只涵盖安装后的 Mac 启动流程；原生 worker 仍需手动接纳并保持活跃。**
写入 Drive 文件不会自动唤醒它。保留 Codex 的 `on-request` 审批和 `workspace-write` 沙箱。

- 默认 **4 小时 / 128 次模型请求**；硬上限 **8 小时 / 128 次**，工具结果续轮也计入预算
- Router 要求保留的 parallel connector-cell runner；不能静默回退到旧串行提交路径
- 未选模型的内部 bootstrap 协议为 V2；显式选择模型/effort 使用 V3。入口消息保留
  `DOTS2CODEX_ROUTER_JOIN_V1` 标记，不表示兼容旧 V1 helper
- 可选[设置辅助工具](docs/gemini_setup/tools/README.zh-CN.md)只由操作者主动运行，不随安装或启动执行

### 4. 为新会话固定真实 native admission 的模型与 effort

```sh
# 离线查看本版本支持的组合；不是实时账号权限检查
python3 -m remote_transport.router_mac models
# 两个参数必须同时给出；也可以使用 -m
./START_ROUTER.command --model gpt-6.1-sol --effort xhigh
```

新会话把选择写入签名 bootstrap，要求 Dots 父上下文**实际提交相同模型、effort 和
`fork_turns="none"` 的 native 接纳**，并将真实返回的任务身份与提交参数记录到不可变 pin。
每轮请求严格核对组合，不支持会话内换模型或 effort；改组合须重新配对，保留旧记录。

官方 CLI 使用私有本地 `model_catalog_json` 提供当前组合；不依赖 `/v1/models` 自动驱动
`/model`，也不只更改显示名称。未选择时保持 `native-subagent-bridge` legacy 模式，
底层模型未知。接纳回执是可信父上下文记录的证据，不是独立底层模型证明。
`ultra` 因 CLI 会改写而不开放；详情见[模型选择与验收边界](docs/MODEL_SELECTION.zh-CN.md)。

## 工作原理

```text
Mac / controller                        已活跃的原生 worker
官方 Codex CLI                          平台真实 admission + 推理上下文
       │                                           │
127.0.0.1 loopback facade                parallel connector cells
       │                                           │
       └──── Google Drive：不可变 JSON 消息 ────────┘
       └──── Google Docs：正式 Control revision CAS ┘

单独的 bootstrap Doc：只用于配对，不代替正式 Control CAS
```

| 路径 | 传输与控制 | 适用边界 |
| --- | --- | --- |
| 本地共享目录 | 私有 POSIX 目录、持久 journal、原有本地协议 | CLI 与 facade 同环境，两端实际能看到同一共享目录 |
| 远程 Drive + Docs | 不可变 Drive 消息、固定 Doc/tab 的 `requiredRevisionId` CAS、每端私有 journal | 两端各自授权并验证精确文件访问；不是把本地目录放进 Drive 同步盘 |

远程 CAS 约束 claim、一次性 begin、结果与交付回执；`duplicate_tolerant` 允许相同内容的物理副本，
本身不是分布式锁。已登记 CAS 的 journal 缺少 CAS 参数时失败关闭，不降级为单 writer 模式。
各端 journal 不可复制、回滚或作为重试捷径删除；只有实际核验结果后才记录 delivery receipt。

长会话支持 1 MiB 完整请求、SSE JSON 心跳、同一任务的迟到结果恢复和显式 `responses_tools`
作用域。工具由 Mac 官方 Codex 按原审批与沙箱执行；未知执行或交付不能自动重放。
原有 `text_only`、`tool_probe`、`repo_review` 范围仍保留。

## 验证状态

**离线测试通过不等于真实账号、Mac 或多小时运行已验收。**

| 范围 | 已有证据 | 尚未证明 |
| --- | --- | --- |
| 原长会话基线 | 2026-09-30 约 28 分钟、4 次真实模型请求全部 `DELIVERED`；含 Mac 工具执行与匹配的 `function_call_output` 续轮；已 CAS 关闭并停止 worker | 多小时存活、完整 128 请求现场容量 |
| 修复版 Router 与并行执行器 | 离线覆盖签名状态历史、私有配对 ledger、双向 probe、启动/停止竞争、PID 身份与未知写入处理 | 新单消息启动的真实 Mac/Google/native 端到端验收、跨 OAuth app 实际互通、真实延迟改善 |
| 每会话模型/effort 固定选择 | 离线覆盖完整组合、真实接纳参数/回执绑定、不可变 pin、请求拒绝及 launcher；官方 CLI 本地目录解析 | 新选择路径的真实 native 推理、Mac/Google 端到端验收；底层实际模型独立证明 |
| 显式模型/effort 选择 | 版本化 25 组合、签名接纳绑定与工具续轮离线测试；官方 CLI 已实际解析全部 25 个目录 | 本环境阻塞实际 HTTP 抓取；真实 native 模型、Mac/Google 新路径与交互菜单仍未验收 |
| Google Cloud / Gemini 指南与可选 helper | 统一 Markdown/HTML/PDF/Word，离线测试与文档版面核查 | 本次未执行真实授权、Google 创建/写入或现场安装 |

此前合并基线的完整离线套件：**478 个 Python 测试 + 25 个 Node 测试**。
新增模型选择路径的验证范围见[模型选择说明](docs/MODEL_SELECTION.zh-CN.md)。
独立离线审查另覆盖 **26 个 Router 案例 + 17 个 helper 案例**；手册完成 27 页版面核查。
详见 [Router 验证](docs/ROUTER_VALIDATION.md)、[指南迁移验证](docs/GUIDES_MIGRATION_VALIDATION.md)
与[历史真实验证](docs/REMOTE_VALIDATION.md)。

并行调度的 866 ms → 444 ms（1.95×）仅是固定虚拟延迟基准，不能作为生产提速数据。
更早的仓库研究取数仍为 `network_unavailable`，不能由 Drive 测试替代验收。

## 文档导航

### 使用与设置

- Router：[安装 / 启动 / 停止](docs/ROUTER_ONE_CLICK.zh-CN.md) · [Dots 配对规程](docs/ROUTER_JOIN_V1.zh-CN.md) · [升级与验收](docs/ROUTER_UPGRADE.zh-CN.md)
- 模型选择：[新会话的 model + effort、真实接纳、官方 CLI 与旧配置兼容](docs/MODEL_SELECTION.zh-CN.md)
- Google Cloud / Gemini：[总入口](START_HERE_GEMINI_GUIDES.md) · [章节索引](docs/gemini_setup/00_README.zh-CN.md) · [离线 HTML](docs/gemini_setup/ALL_GUIDES.html) · [PDF](docs/gemini_setup/HANDBOOK.pdf) · [Word](docs/gemini_setup/HANDBOOK.docx)
- Gemini 辅助：[启动提示词](docs/gemini_setup/PROMPT_GEMINI_START.txt) · [13 段提示词](docs/gemini_setup/02_GEMINI_PROMPTS.zh-CN.md) · [可选设置 helper](docs/gemini_setup/tools/README.zh-CN.md)
- 手动远程：[安装配置](docs/REMOTE_SETUP.zh-CN.md) · [长会话与恢复](docs/REMOTE_LONG_SESSIONS.zh-CN.md) · [活跃 worker 契约](docs/ACTIVE_CONNECTOR_WORKER.md)
- 本地模式：[完整说明](LOCAL_BRIDGE_README.zh-CN.md) · [角色与启动顺序](START_HERE.zh-CN.md) · [本地自动部署提示词](docs/AUTO_DEPLOY.zh-CN.md)

### 设计、验证与安全

- [Drive 设计](DRIVE_DESIGN.md) · [普通单 writer 模式](DRIVE_RUNBOOK.md) · [Docs CAS](DOCS_CAS_RUNBOOK.md)
- [并行连接器与 timing](docs/CONNECTOR_LATENCY.md) · [离线延迟验证](docs/LATENCY_VALIDATION.md) · [本地验证](docs/VALIDATION.md)
- [会话固定分配](ROUTING_RUNBOOK.md) · [固定 nonce 工具验证](TOOL_PROBE.md) · [有界仓库研究](REPO_REVIEW_RUNBOOK.md)
- [安全边界](SECURITY.md) · [角色契约](ROLE_CONTRACT.json) · [免责声明](DISCLAIMER.md) · [第三方说明](THIRD_PARTY_NOTICES.md)

## 安全与排障

- **最小权限与本人授权。** Router 当前需要 Mac 自己已有的 `drive.file` 和 `drive.readonly`。
  `drive.readonly` 是较广的 Drive 只读权限，不限于专用 folder；必须明确审阅并批准。
  `drive.file` 不因选中一个文件夹就获得另一 OAuth app 创建的全部子文件访问权
- **凭据与配对信息保密。** 不共享 OAuth/token，不把 join-code、任务正文、runtime、journal 或真实资源 ID
  提交到仓库、公开 issue、日志或截图。join-code 不放 shell 参数，使用规程要求的私有文件
- **完整性不等于保密性。** HMAC 不是加密或平台身份认证；清空 bootstrap 正文不会删除 Google Doc 历史版本。
  folder 和 Docs 仅向已获授权的参与方开放
- **`RECOVERY_REQUIRED` 时停下。** 保留私有 journal、操作 ID 和响应，按精确证据对账。
  不盲目重试未知 create/upload/CAS，不更换 receipt 或新建任务重复执行
- **关闭需要分开核验。** `closed=true` 是正式 Control CAS 已关闭；还要检查 `process_stopped`。
  `worker_stop_confirmed=false` 表示未获得平台停止确认。关闭可阻止新的 begin，已消费的执行仍可能完成
- **版本、访问或状态异常。** 按[常见问题](docs/gemini_setup/05_TROUBLESHOOTING.zh-CN.md)
  与[升级验收清单](docs/ROUTER_UPGRADE.zh-CN.md)核查。不要绕过权限拒绝或系统安全警告

## 开发与离线测试

在已允许安装依赖的环境中：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-test.txt
# 可选：SDK 离线测试需要这些依赖；不做真实授权
python3 -m pip install -r requirements-google-example.txt

python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s remote_tests -v
python3 -B -m unittest discover -s remote_audit/cas -v
python3 -B -m unittest discover -s remote_audit/drive -v
python3 -B -m unittest discover -s sdk_tests -v
python3 -B -m unittest discover -s docs/gemini_setup/tools -v
node --test native_connector/runner.test.js native_connector/test_adapter.js
node native_connector/benchmark.js
```

开发验证使用 Python 3.12；完整测试需要 `jsonschema` 4.x，Node 测试需要支持 `node --test` 的 Node.js。
helper 测试模拟 OAuth、浏览器与网络。Google SDK 示例的额外依赖与 Router 安装依赖分开声明；
requirements 固定的是所选直接依赖，不是完整的传递依赖锁文件。

公开文件清单和 SHA-256 在 [ROUTER_PACKAGE_MANIFEST.json](ROUTER_PACKAGE_MANIFEST.json)。
清单排除自身；源码树摘要与 ZIP 文件摘要不同。部署前核对两端版本及公开文件 hash。

## License 与免责声明

本仓库保留原有 [MIT License](LICENSE) 和 [第三方说明](THIRD_PARTY_NOTICES.md)。
新增[免责声明](DISCLAIMER.md)说明实验性质、服务条款与账号责任、安全和验证限制，
不修改原有许可证，也不是安全、合规或生产适用性保证。

## English summary

Dots2Codex is an independent experimental bridge between the unmodified official
Codex CLI and an already active, authorized native inference worker. It preserves
the local POSIX bridge and adds a Drive/Docs revision-CAS transport, bounded long
sessions, Mac tool continuations, and a one-message Router pairing flow.

The historical baseline completed four live requests including one Mac tool cycle.
The repaired Router, parallel executor, and optional setup helper are offline
validated; new end-to-end live acceptance and multi-hour endurance remain pending.
Router defaults to four hours / 128 model requests, with an eight-hour / 128-request
hard maximum. Native admission remains manual. No model entitlement, free quota,
automatic wake, uptime, exactly-once external effects, or security guarantee is
provided. Read the [disclaimer](DISCLAIMER.md) before operating it.
