# Direct 本机脱敏诊断

在包含本功能的版本中，可从仓库目录运行：

```sh
./DIRECT.command diagnostics
./DIRECT.command diagnostics --lines 200
```

默认最多 80 条；`--lines` 接受 1–200。若启动时使用过自定义状态目录，诊断时使用同一个目录：

```sh
./DIRECT.command diagnostics --state-dir "/你的私有状态目录" --lines 80
```

命令只读本机状态和脱敏事件，不启动、停止或重启服务，不连接 HTTP/MCP/tunnel，不读取密钥，不读取请求数据库内容，不发出模型或工具请求。它只需 Python 3.11+ 标准库，不要求 Pillow、MCP、tomlkit、网络或运行中的服务。显示的 service stage 是已记录状态，进程字段仅检查已记录 PID；不代表健康探测通过。

## 何时开始记录

新版本的共享服务运行后自动生成事件。尚未运行过新版本、仍在运行旧版本、从未配置过或没有事件时，命令显示 `No diagnostic events available`。它不能重建旧版本丢弃的输出，也不会为了获取日志重启现有服务。更新文件不等于更新已经运行的进程；需另行明确选择正常 Stop/Start 才会切换运行代码。不要为此删除数据库或重建未确认执行结果的动作。

## 存储、保留与隐私边界

默认存储在 `~/Library/Application Support/Dots2Codex Direct Global/bridge/diagnostics/`。自定义状态目录时位于其 `bridge/diagnostics/`；手动运行全局 MCP 入口时，位于所选全局数据库旁的 `diagnostics/`。目录权限 0700，文件 0600，拒绝符号链接、硬链接、非普通文件或不安全权限。

- 仅保留 `events.jsonl` 和 `events.jsonl.1`，每个最多 256 KiB，共最多 512 KiB。写满时丢弃较旧文件。没有按天数删除的承诺；低流量时旧事件可能保留较久。
- 另有 32 字节随机 `correlation-salt.bin` 和零字节 writer lock。salt 仅用于本机诊断关联，不是认证凭据，不改变服务权限。相同逻辑 ID 跨进程、跨重启得到相同短 HMAC 引用；每个进程另有随机 `process_ref`。不要分享 salt。
- 事件只含固定操作/方法/阶段/结果/错误码、UTC 时间、单调时钟毫秒、有限时长/序号/HTTP 状态及不可直接还原的逻辑 ID 引用。没有原始 route/request/action ID、身份、路径、请求内容、查询、工具参数或结果、图片、响应、环境变量、bearer/claim/context/operation token、异常文本。
- 不收集官方 tunnel 或子进程的原始 stdout/stderr。已有禁止原始日志和依赖调试日志的保护保持不变。
- 读取时再次按字段类型与固定允许列表过滤。损坏、超大或权限不安全的内容不会原样显示。
- 记录为 best effort：调用处先只保留允许字段，再放入最多 256 条事件的非阻塞内存队列；独立 daemon writer 负责文件 I/O，协议执行和停止流程不等待磁盘或清空队列。队列满、磁盘/权限问题或并发锁占用时可丢失事件，崩溃时最后一行可能不完整。它不是审计账本。缺少事件不能单独证明操作没有发生。日志失败不会回滚、重复执行或授权重试动作。

诊断输出仍会透露操作时间和频率。分享前可先在本机查看；不要发送整个私有状态目录、数据库、env 或 salt。

## 如何看一次失败

按 `route_ref`、`request_ref`、`action_ref` 关联事件；`call_ref` 区分单次 HTTP/MCP 调用，`process_ref` 区分进程，`event_seq` 是进程内顺序。UTC 可与客户端时间对照；单调时钟适合本次系统启动内的先后与时长，不能跨系统重启直接比较。

典型阶段：

1. `http begin`：本机 HTTP handler 收到请求。`ingested`：校验并入队成功，含该次 `wait_ms`。
2. `mcp begin/queued/invoke_begin`：SDK 已分派工具调用、进入工作队列、开始运行。`runtime_wait begin/end` 显示等待时长，以及 ready/pending/closed 等结果；pending 的 reason 区分 deadline、cooperative_cancel、service_closed。
3. `response_commit end`：原生回复已写入持久状态；outcome 仅区分 message/tools，不记录回复内容。
4. `http delivery_fenced`：发给客户端前的持久防重发标记已写入。随后 `socket_flushed`（SSE）或 `socket_written`（JSON）仅说明本机写出没有抛出错误，不证明客户端已经接收或执行。
5. `http timeout`、`http_status:504`、`error_code:outcome_pending`：这一 HTTP 等待窗口已到期。若相同 `request_ref` 的 response_commit 晚于 timeout，可定位为回复晚于客户端窗口；并不自动恢复该轮。
6. `http disconnect`：本机读写抛出 socket 错误，结果仍可能未知。有些断连直到后续写入才被发现，不能把事件时间当作远端点击/断网时间。
7. `mcp timeout` 与 `end outcome:timeout`：适配器自己的超时；工作线程的 invoke_end 可能更晚。`end reason:coroutine_cancelled` 仅证明适配器观察到协程取消，不能证明是用户点击取消，更不能确定上游取消原因。没有 MCP begin 也不能据此确定是用户、客户端或隧道的问题。

诊断不修改 API 语义、防重发标记或执行安全策略。不要把 timeout/cancelled 当作回滚，不要新建 action ID 重做未知动作。HTTP 传输重试的独立配置与边界见 [HTTP_REPLAY.md](HTTP_REPLAY.md)。
