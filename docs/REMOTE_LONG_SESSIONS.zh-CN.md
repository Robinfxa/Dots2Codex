# 手动启动原生 worker 的多小时远程会话

这是新增的实验性长会话实现。它保持一个已经由平台接纳的真实原生 worker，
Mac 端运行未修改的官方 Codex CLI；Drive 保存不可变消息，Docs CAS 控制每次执行。
无需 Mac 自动唤醒 worker。原生 worker 必须始终处于平台允许的活跃执行上下文中。
Python 不会启动、恢复、唤醒原生模型，也不能保证平台让一个上下文连续在线八小时。

## 实现边界和验证

- 长会话显式启用 `--long-session` 且必须使用 Docs CAS；旧文本烟测入口保持原行为
- pin 可配置 1 秒至 8 小时、1 至 128 次模型请求；默认仍为旧的 600 秒 / 3 次
- 一次完整请求（所有工具定义、指令、文本历史、工具输出）最多 1 MiB
- 单个结果最多 128 KiB，普通文本结果另受原有 16,384 字符限制
- 每个端点最多 64 MiB 累计不可变 transcript，journal 上限 96 MiB；新请求预留 3 MiB 后才准入
- 控制 Doc 上限 1 MiB，操作账本最多 912 条；不删除历史，不压缩或丢弃已消费许可
- 同时只允许一个模型请求；一个请求只返回一个消息或一个工具调用
- 128 次是模型请求数，工具结果后的继续推理也占一次。上下文超限会提前拒绝，不能无限做事
- 默认 worker 每 15 秒空闲轮询，最多 4,096 次控制读取；Mac 默认每 5 秒检查结果
- HTTP 立即返回 SSE，定期发送 JSON `response.in_progress` 事件。仅发 SSE 注释不能重置官方 Codex 的解析事件 idle timeout

仓库回归使用合成结果与离线假 Drive/Docs；这些测试本身不发起真实模型推理、调用 Google
服务或执行客户端工具。另一次 2026-09-30 的真实长会话基础版验收持续约 28 分钟：Mac
controller → Google Drive/Docs → 原生 worker 共 4 次请求全部 `DELIVERED`，包含一次
真实 Mac function 工具意图、匹配的 exit 0 工具输出及原生继续推理。控制端已 CAS 关闭
会话，worker 已停止。[公开验证摘要](REMOTE_VALIDATION.md)区分这次现场验收和离线回归。

这次现场验收没有使用本次新增的并行连接器执行器；后者目前仅离线验证。本版本尚未证明
几小时实际平台在线率、完整 128 次请求、通用跨 OAuth app 互通或真实加速。
连接器及多次 CAS 往返可能仍需数分钟。

## 两端安装

两端使用同一固定版本的完整仓库和 Python 3.11+。在各自允许安装软件的环境运行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-remote.txt
```

`responses_tools` 运行时也需要 `jsonschema` 4.x，已固定在 `requirements-remote.txt`。
只有 Mac 的独立授权客户端还需要：

```sh
python3 -m pip install -r requirements-google-example.txt
```

Mac 保留自己的 OAuth。原生端只使用它已经连接并获准使用的 Drive/Docs 连接器，
不在云沙箱里复制 OAuth、不启动浏览器 callback、不创建新凭据或扩展权限。
Google 资源准备、最小权限与跨 app file-ID 可读性检查沿用
[原配置指南](REMOTE_SETUP.zh-CN.md)。不要重置正在使用的控制 Doc。

## 1. 先手动启动真实 worker，然后创建新会话

让父助手用平台真正支持的原生任务工具创建一个活跃 worker。传递
[worker 操作契约](ACTIVE_CONNECTOR_WORKER.md)，先不传用户请求内容。
记录工具实际返回的 native task identity；不得用一个希望存在的名称代替。

在已获授权的控制端创建私有目录和新 pin。例如四小时、128 次，允许 Codex 工具：

```sh
umask 077
mkdir -m 700 "$HOME/.config/dots2codex/session-NEW"
RUNTIME="$HOME/.config/dots2codex/session-NEW"
python3 -m remote_transport.cli new-deployment \
  --pin "$RUNTIME/pin.json" --session SESSION_ID \
  --native-task-id ACTUAL_TOOL_RETURNED_NATIVE_ID \
  --seconds 14400 --max-requests 128 --scope responses_tools
python3 -m remote_transport.cli provision-journal \
  --pin "$RUNTIME/pin.json" --journal "$RUNTIME/controller" --role controller
```

如果只要文本，使用 `--scope text_only`。开启工具作用域意味着原生 worker 可以返回
Mac Codex 实际公布的 function/custom 工具意图，Mac 自己依照原审批和沙箱执行。
不是远程任意 shell daemon。不要关闭 Mac 的审批、沙箱或安全提示来让会话继续。

通过可信渠道把**同一个 pin**与以下非凭据配置交给已启动的 worker。
复制 pin/config，不复制角色 journal、OAuth 或消费标记：

```json
{
  "document_id": "EXACT_CONTROL_DOCUMENT_ID",
  "tab_id": "EXACT_TAB_ID",
  "control_id": "UNIQUE_CONTROL_ID",
  "writer_identity": "WORKER_CONNECTOR_LABEL",
  "folder_id": "EXACT_MESSAGE_FOLDER_ID"
}
```

将 pin 对应的初始控制状态写入事先批准的专用空白单-tab Doc，仍需操作者批准精确
目标与写入。这不是 `connector_worker init` 的副作用。可使用原配置指南的
`examples.remote_setup init-control`，或用原生连接器按既有 Docs CAS 初始化规范执行。
如果旧会话已 DELIVERED，也不要自动清空它；先保留证据并另行批准新会话资源。

worker 在自己的私有本地目录运行（真实身份必须匹配 pin）：

```sh
python3 -m remote_transport.connector_worker init \
  --root WORKER_NEW_RUNTIME --pin PIN_FILE --config CONFIG_FILE \
  --native-task-id ACTUAL_TOOL_RETURNED_NATIVE_ID --poll-seconds 15
```

只有 worker 已核对 Doc、pin、实际身份并开始轮询后，才告诉 Mac “ready”。
`init` 成功本身不是在线/原生接纳证明。

## 2. Mac 启动 facade 和官方 Codex

凭据位置使用原配置指南已批准的 `DOTS_GOOGLE_AUTHORIZED_USER_FILE` 等配置。
在 Mac 的第一个终端启动：

```sh
python3 -m remote_transport.cli serve \
  --pin "$RUNTIME/pin.json" --journal "$RUNTIME/controller" \
  --transport drive --folder-id EXACT_MESSAGE_FOLDER_ID \
  --client-factory examples.google_clients:create_drive_client \
  --drive-mode duplicate_tolerant \
  --docs-client-factory examples.google_clients:create_docs_client \
  --control-document-id EXACT_CONTROL_DOCUMENT_ID --control-tab-id EXACT_TAB_ID \
  --control-id UNIQUE_CONTROL_ID --control-writer-identity MAC_WRITER_LABEL \
  --long-session --deadline 1800 --poll-interval 5 --heartbeat-interval 15 \
  --port 8765 --ready "$RUNTIME/ready.json"
```

deadline 是单个 HTTP 请求等待预算，不是重新推理的倒计时；设为 1800 表示等最多 30 分钟。
pin 的到期时间独立控制新准入/claim/begin。到期后 facade 仍可只读取回已完成结果，
需要操作者 Ctrl-C 停止它。ready 会打印 pin、作用域、截止时间、请求和容量限额。

第二个 Mac 终端先检查安装的 CLI（只运行 --version/--help，不推理），再获取命令：

```sh
python3 -m remote_transport.operator check-codex --ready "$RUNTIME/ready.json"
python3 -m remote_transport.operator codex-command \
  --ready "$RUNTIME/ready.json" --workdir /ABSOLUTE/PATH/TO/YOUR/PROJECT
```

运行返回的 `command`。它使用 `native-subagent-bridge`、Responses HTTP、无 OpenAI
Bearer 凭据的 loopback 自定义 provider，禁用 HTTP/stream 自动重试与 request compression，
保留 `on-request` 审批和 `workspace-write` 沙箱，不修改用户全局 Codex 配置。
命令从已安装的 `codex` 启动；仓库不捆绑 Codex 或模型。
先检查 `codex --version`。本次协议源码审阅对应 rust-v0.159.2；其他版本须重新现场验收。
命令还禁用 multi-agent、无界重连、内置 web search/image/computer 表面；仅支持公布的 function/custom 工具。

首次真实请求固定官方 CLI 发出的 `session-id` 和 `thread-id`。同一个运行中的 Codex
会话可以多轮使用；另一个进程或另一个 thread 不能冒用此 pin。重启 facade 可沿用同一
journal 进行结果恢复；不保证另一个 Codex 进程自动接续原来的 client identity。
重启时 ready 使用新文件路径，不能覆盖一个还在使用的 ready 文件。

## 3. 状态、迟到结果和明确关闭

无需手抄 HTTP header。operator 从本机持久 facade 状态读取真实 client identity：

```sh
python3 -m remote_transport.operator status \
  --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller"
python3 -m remote_transport.operator result \
  --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller" --request-id REQUEST_HASH
```

状态列出 request hash、交付状态和截止时间；HTTP 响应的 `X-Request-ID` 也携带相同 ID。
`result` 只读，返回确切 result hash 和输出。真的看到了最终文本以后才能 ack：

```sh
python3 -m remote_transport.operator ack \
  --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller" \
  --request-id REQUEST_HASH --result-id EXACT_RESULT_HASH \
  --evidence 'I read the exact recovered final text'
```

工具结果不能用这条人工 ack 冒充实际执行。下一条模型请求必须含完全匹配的已发出
call_id/工具意图与 Mac 实际工具输出。不能从“socket 已 flush”推断工具已经执行。

```sh
python3 -m remote_transport.operator close \
  --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller" --confirm
```

close 在同一控制记录上 CAS 封闭新 admit/claim/begin，因此可以与 worker 的 begin 竞争。
如果 begin 已成功，close **不能撤销已发出的许可或正在运行的工具**；会返回
`execution_may_be_running=true`，仍保留结果恢复/回执能力。Ctrl-C facade 只是停止本地
HTTP 服务，不是远端取消。worker 的本地 `stop` 也不是跨端 fencing，应先使用 CAS close。

## 故障规则

- 重试完全相同的文本请求只找回同一个 durable job，不新建执行
- 工具意图在发送前先持久化 emission intent；任何不确定交付都禁止自动重发工具
- begin 未知、工具结果未知、缺失 journal、控制 epoch 回退、hash/元数据冲突：停止并核对；不新建任务“再试一次”
- 已获得的结果可在 pin 到期/close 后按固定字节继续发布与提交；不再次推理
- 未知 upload 只核对该固定对象和已知 file ID；不能凭一次空搜索盲重传
- 确认的 stale-revision rejection 才表示该次 CAS 没运行；超时/缺少 count 均不等于失败
- 工具调用必须是实际 request 中公布的名字/namespace/type。function 参数按公布 JSON Schema 验证，外部引用拒绝；custom input 由 Mac 的原生工具 parser/审批处理
- 多模态、并行工具意图、加密推理、previous_response_id 增量模式、自动 compaction、自动 worker 接管、自动恢复未知副作用均不支持

请保留私有 runtime 证据。此实现没有 exactly-once 外部副作用保证，也不把一个 native
上下文、Drive 上传或网络重连当作可随意重放的动作。
