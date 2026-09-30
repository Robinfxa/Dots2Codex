# Dots2Codex

一个实验性的、基于共享文件的文本推理桥：官方 Codex CLI 在桌面端运行，已有的活跃原生 agent 在 broker 端处理请求。两端通过私有共享目录交换数据，无须共享网络命名空间。

项目提供隔离启动器、loopback Responses facade、文件队列、角色交接和保守恢复协议。它没有内置模型、原生 agent API 或永久后台调度器。

```text
桌面环境                         broker 环境
官方 Codex CLI                  已活跃、获授权的原生 agent
      │ 本地 HTTP                         │
127.0.0.1 facade                claim → read → 推理 → complete
      └──────── 私有共享文件目录 ──────────┘
```

**默认范围：text-only、有界运行。** 可显式启用同一用户的多会话固定分配，以及两种严格受限的工具测试。已观察到实际桌面文本、固定 nonce 命令和安全空闲换人的往返；仓库研究的真实取数仍被网络阻塞。没有证明通用工具执行、用户 Mac 部署或全天候无人值守服务。

## 这能做什么

- 为一次新运行生成独立 HOME/CODEX_HOME/workspace，显式选择桥接 provider，检查失败即停止
- 保留官方 CLI 的只读 sandbox 与 on-request 审批，不修改已有默认配置
- 使用原子文件写入、锁、请求哈希、租约和 epoch 拒绝陈旧提交
- 先标记推理开始，再交出请求；结果不明时阻止不安全的重复调度
- 用版本化角色契约交接给另一位操作者或活跃 agent
- 区分本地 outbox、观察者读到、父级明确确认；不把“写了文件”说成“已送达通知”

## 前提

- Python 3.11+ 与 POSIX 文件系统，支持 `flock` 和原子替换；主要验证环境是 Linux
- 桌面端已安装来自官方来源的 Codex CLI；实际文本测试使用过 `0.159.0-alpha.7`，兼容性源码核对还覆盖 `0.159.2`。其他版本须自行验证
- CLI 与 facade 必须处在能互通 loopback 的同一环境
- 两端都能读写同一私有共享目录；本地绝对路径可以不同
- 完整测试及可选 `tool_probe` / `repo_review` 范围需要 `jsonschema` 4.x；纯文本运行只依赖标准库。先运行 `python3 -c "import jsonschema"` 检查；若缺少，按当前环境的安装授权规则使用 `python3 -m pip install -r requirements-test.txt`
- broker 必须是已经活跃、获授权、具备实际推理能力的原生 agent。普通 Python 进程不会因此获得平台任务工具或模型权限

仓库不包含 Codex 二进制、凭据、认证配置或真实会话记录。

## 一键部署提示词

复制下面这段话给另一位 dot，让它从下载源码开始完成部署与验证。完整流程单独放在[自动部署任务说明](docs/AUTO_DEPLOY.zh-CN.md)。

```text
请从 https://github.com/Robinfxa/Dots2Codex 下载项目到新的工作目录，记录提交版本并阅读同一版本的 docs/AUTO_DEPLOY.zh-CN.md，再按其中流程，在你自己的已授权云端桌面和命令环境中自动完成检查、两端部署、一次真实文本验证和清理。能独立完成的步骤直接做；缺少必要能力或权限时，报告具体阻塞和最小下一步。不要默认操作我的电脑。
```

## 快速开始

先运行合成测试，再按角色部署：

```sh
python3 -m unittest discover -s tests -v
```

### 1. 选择新部署目录

以下变量是示例，请在两端分别设置为各自可见的同一共享目录。`BRIDGE_ROOT` 必须尚不存在，由初始化命令创建。不要预建内部 queue 或 isolated-cli 目录。

```sh
export BRIDGE_ROOT="/absolute/private/shared/new-deployment"
export OWNER="demo-owner"
python3 portable.py --root "$BRIDGE_ROOT" init --owner "$OWNER" --seconds 600 --max-requests 3
```

### 2. 核实文件通道

桌面端：

```sh
python3 probe.py --root "$BRIDGE_ROOT" observe --role desktop
python3 probe.py --root "$BRIDGE_ROOT" offer
```

broker 端：

```sh
python3 probe.py --root "$BRIDGE_ROOT" observe --role broker
python3 probe.py --root "$BRIDGE_ROOT" answer
```

桌面端在120秒内完成：

```sh
python3 probe.py --root "$BRIDGE_ROOT" verify
```

该探针只证明共享文件 nonce 往返，不证明模型槽位、网络访问或通知渠道可用。

### 3. 让实际活跃的 broker 就绪

```sh
python3 portable.py --root "$BRIDGE_ROOT" assign --owner "$OWNER" --role broker --worker broker-one --native-capability-attested --save "$BRIDGE_ROOT/evidence/broker-assignment.json"
```

`--native-capability-attested` 是操作方的能力声明，不是 Python 检测结果。broker 接着按 [BROKER_HANDOFF.zh-CN.md](BROKER_HANDOFF.zh-CN.md) 进入有限等待，并负责实际推理和提交。

### 4. 在桌面端启动独立 CLI

先查看计划：

```sh
python3 desktop_bootstrap.py --root "$BRIDGE_ROOT" --owner "$OWNER"
```

选择获授权的官方可执行文件后，显式启动：

```sh
export CODEX_BIN="/absolute/path/to/official/codex"
python3 desktop_bootstrap.py --root "$BRIDGE_ROOT" --owner "$OWNER" --codex "$CODEX_BIN" --execute
```

只有 facade 和实际 broker 通过新随机就绪挑战才启动。普通 `codex` 命令仍使用原有配置。正常结束请退出该 CLI；到期清理只处理本次启动器创建的子进程。

完整顺序见 [START_HERE.zh-CN.md](START_HERE.zh-CN.md)。首次接手可先做 [确定性交接练习](HANDOFF_TEST.zh-CN.md)，它明确不调用真实模型。

## 可选：会话固定分配与有界工具循环

默认快速开始和自动部署提示词仍只做文本验证。需要扩展时，先阅读相应 runbook，在新 registry 中显式选择范围：

- `text_only`：每个会话最多 3 次模型请求，独立队列和固定原生 worker；多个会话可并行，同一会话串行
- `tool_probe`：仍最多 3 次模型请求，只允许一条固定 nonce 命令；由桌面 CLI 执行，worker 读取关联输出后回复
- `repo_review`：最多 16 次模型请求、12 次工具调用、900 秒；只允许固定 helper 匿名读取本公开仓库，先固定 commit，再读或搜实际目录中的文件；禁止中途更换 worker

首次分配或明确允许的安全换人仍需要活跃协调方调用环境实际提供的原生任务工具。普通后续消息和工具回执直接进入已绑定会话，无须逐条调用模型路由器。Python 本身不创建原生 agent，文件写入也不会自动唤醒协调方。

严格工具范围不是通用 shell 或任意长任务服务。真实仓库研究运行只完成了一次失败的 `tree` 调用，返回 `network_unavailable`；未取得任何仓库内容或 commit，不能视为成功的多轮研究。详见[验证摘要](docs/VALIDATION.md)。

- [固定分配设计](ROUTING_DESIGN.md)、[操作流程](ROUTING_RUNBOOK.md)与[契约](ROUTING_CONTRACT.json)
- [单命令工具验证](TOOL_PROBE.md)
- [有界仓库研究流程](REPO_REVIEW_RUNBOOK.md)

## 文档和契约

- [broker 交接](BROKER_HANDOFF.zh-CN.md)：claim、read、结果提交、续租与停止
- [通知与恢复](RECOVERY.zh-CN.md)：不确定调度、回执和有界重试
- [角色契约](ROLE_CONTRACT.json) 与 [JSON schemas](schemas/)：`file-ipc-portable/1`
- [验证摘要](docs/VALIDATION.md)：测试范围与仍未验证的能力
- [安全边界](SECURITY.md)
- [来源与第三方说明](THIRD_PARTY_NOTICES.md)

## 限制与免责声明

本项目是实验性工程原型，按 MIT 许可证“按现状”提供，不保证适用于生产、24/7 可用、通知及时送达、恢复成功或任何模型输出正确性。当前没有证明通用工具调用及执行的多轮闭环；不能把文本测试当作完整代理能力证明。

本项目不是 OpenAI、ChatGPT、Codex 或其他服务提供方的官方产品，不代表其背书、合作或支持承诺。相关名称与商标属于各自权利人。

**桥接不等于免费推理。** 原生任务、模型调用、重试、订阅或第三方基础设施仍可能消耗配额、产生费用或受限流约束。项目不承诺零 token、免费额度、绕过计费或提升账户权限。

使用者应确认自己有权访问相关环境和数据，遵守适用法律、各服务条款及账户限制，并对部署、授权、费用和输出使用负责。不得用本项目规避安全保护、权限拒绝、配额、访问控制或其他限制。需要新授权时应停止处理并通过支持的流程取得授权。

## License

原创代码与文档采用 [MIT](LICENSE)。`vendor/` 是本项目早期 Python 实现的冻结副本；没有附带外部 Codex/CC Switch 实现。外部软件及服务保留自己的许可证和条款，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## English summary

Dots2Codex is an experimental, single-user shared-file bridge with text-only defaults between an unmodified official Codex CLI and an already active, authorized native inference agent. The CLI and loopback facade run together; the broker exchanges data through a private directory. Optional bounded scopes add sticky per-session workers, one fixed nonce command, and a fixed public-repository reader. Successful multi-read repository research remains unverified because the live fetch was network-blocked. No model backend, native-task endpoint, permanent scheduler, authentication credentials or free inference entitlement is bundled. Keep sandboxing and approvals enabled, verify the actual environment, and do not treat local outbox writes as delivered parent notifications.
