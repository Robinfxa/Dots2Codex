# Direct 云端无人值守测试与控制循环

## 结论边界

这里的「无人值守」指：已在运行的原生父控制器自行观察队列、创建或恢复同一个原生子线程，在规定测试场景完成前不需要用户再发消息。它不表示平台已经安装永久调度器，也不表示 MCP 可以唤醒已经结束的父线程。

- `automatic_wake: false` 必须保持。Python、MCP 通知与 5 秒长轮询均不创建模型，也不能证明原生唤醒。
- 当前可复现的云端测试链：真实原生子线程 → 私有测试邮箱（或同一网络命名空间内的 localhost 测试 RPC）→ 官方 SDK MCP stdio → 实际 `GlobalRuntime` → 合成 Codex HTTP 客户端。
- 替代的只有控制工具入口和 Codex 客户端。原生任务须由宿主的实际创建工具启动，答案须由该任务产生；本夹具没有模型 API、答案生成器或原生 spawn 实现。
- 此测试不证明 Mac Desktop、正式隧道、已部署 MCP 到原生线程的唤醒、工具实际在 Mac 执行、跨平台上下文压缩恢复或长期无人值守可靠性。
- 桥接状态中的 `actual_native_platform_verified: false` 保持不变。独立平台创建/恢复回执才能支持「实际原生任务参与」的结论，不能用逻辑 worker_id 自证。

## 可重跑的回归测试

在包根目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 direct_bridge/orchestration/test_unattended_fixture.py -v
```

这五项回归测试完全是合成工作线程，不是原生验收：

1. 延迟入队、同一归属多轮、空闲 `pending`、真实 MCP 子进程重启、原 SQLite/claim 续接、纯文字 HTTP 重取、重复 finish 去重、改变 action 内容/worker/epoch 拒绝。
2. 场景重复启动、未结清时重启、未重启就续跑等控制顺序拒绝。
3. 报告保留未验证边界且不泄露测试 bearer。
4. 拒绝复用或覆盖已存在的测试状态；使用全新空目录。
5. 真实夹具 CLI 的私有邮箱往返、0600 ready 文件、未知命令拒绝与正常退出。

夹具 MCP 接收超时为 8 秒，高于接口允许的 5 秒等待；邮箱客户端观察上限为 15 秒。回归测试包含完整 5000ms 空闲等待。

已有 `facade/test_global_runtime.py` 和 `tests/test_global_mcp.py` 另外覆盖含工具响应的发射栅栏、断线后禁止重发、工具回传及多路并发；这些也是合成客户端/工作线程证据，不能合并称为真实 Mac 工具验收。

## 真实原生三轮试验

本流程仅创建临时私有测试状态，不配置隧道、凭据、登录、用户 Codex 或系统服务。先确认云端工作目录可写且官方 MCP SDK 已可用。给实际原生任务保留一个并发槽位。

1. 创建全新的 0700 目录。运行夹具，保留其进程句柄：

   ```sh
   mkdir -m 700 /tmp/direct-native-test
   python3 -B direct_bridge/orchestration/unattended_fixture.py serve \
     --state-dir /tmp/direct-native-test \
     --ready-file /tmp/direct-native-test/ready.json \
     --lifetime-seconds 1200
   ```

   默认私有文件邮箱适用于不同 shell 调用具有隔离 localhost 的云环境。同一网络命名空间可明确使用 `--transport http`。这两种都只是测试入口，不是正式 MCP 安装/同步。目录与 ready 文件不得共享给第三方。

2. 父控制器通过夹具 `rpc ... begin` 安排两秒后第一轮；再用 `rpc ... call --json '{"name":"bridge_status","arguments":{}}'` 观察待处理路由。不能凭造一个 route_id。

3. 用实际宿主创建工具创建一个原生子线程，明确指定状态提供的模型/effort（当前夹具为 `gpt-6-astra` / `xhigh`），保存平台实际返回的任务标识。不接受静默降级；模型参数的创建回执不等于底层运行时密码学证明。父控制器把实际 route_id、子线程自己的平台标识和新的稳定 epoch 交给它。

4. 子线程通过夹具调用 `get_request` 首次认领，读取完整上下文并自己回答。用准确 scope、稳定 action_id、合规 Responses 结构调用 `finish_request`。将完全相同参数重复一次，检查是同一提交。第二轮在收到第一轮实际 HTTP 响应后自动延迟两秒入队；子线程在同一上下文用 claim + after_seq 继续，必须取得 delta。第二轮后执行一次 3000ms 空闲等待，取得 `pending / automatic_wake:false` 后正常结束这个原生 turn，保留任务身份和上下文。

5. 父控制器确认 `report.stage = awaiting_restart`、两次实际 HTTP 交付和两个唯一提交。执行 `rpc ... replay` 重取纯文字，`rpc ... restart` 重启夹具自己的真实 MCP/桥接进程，再次 `replay`。原 SQLite、claim、归属与去重记录不变，不创建第二个原生任务。

6. 执行 `rpc ... continue`，第三轮两秒后入队。父控制器在状态中观察原已认领路由的 `request_pending`，用宿主原生 follow-up/send-message 机制恢复原任务。子线程使用原 claim/epoch、after_seq=2，取得第二个 delta，依靠保留的原始上下文回答第三轮，并重复提交相同 finish 以检查去重。不要将完整历史重新灌入子线程来伪装连续性。

7. 父控制器验证 `report.stage = complete`、无 failures、三轮答案都正确、同一 worker、full=1、delta=2、response_commits=3、实际 HTTP 交付已发生。六次相同配对 finish 调用不应产生六次提交。再重取第三轮纯文字，提交数仍为三。

8. 保存不含 claim/bearer 的报告及独立原生创建、空闲、恢复回执。用 `rpc ... stop` 关闭夹具自身进程，确认结束；默认最多运行 1200 秒，允许 60–3600 秒。不要停止任何非夹具进程。未知结果不应直接重做邮箱请求或原生 spawn。

所有命令的通用客户端形式是：

```sh
python3 -B direct_bridge/orchestration/unattended_fixture.py rpc \
  --ready-file /tmp/direct-native-test/ready.json OPERATION
```

`call` 额外接收 `--json '{"name":"工具名","arguments":{...}}'`。`report` 不含私有认证与 claim；测试目录中的配置、ready 与子线程收据可能含测试令牌，不应提交到仓库或报告附件。

## 实际父控制器的运行规则

正式运行仍使用已安装的八个 Direct MCP 工具，不能把测试邮箱接到生产。

1. 启动时核对真实可见的 schema、mode、config_id、instance_id、allowed_pairs 与平台并发额度。恢复时先查看原控制记录及实际任务状态，不能仅因 instance_id 改变就替换所有子线程。
2. 每轮先读取 `bridge_status`。新路由由 `pending_routes` 识别；已认领路由的新请求由 `routes[].request_pending` 与 `last_seq` 识别，不能只盯 `pending_routes`。
3. 以 `(config_id, route_id)` 保存原生创建意图、实际任务 ID、模型/effort、epoch 与已观察序号。先记录创建意图再调用创建工具；调用结果未知时进入「待核实」，检查宿主任务列表/回执，不盲目再 spawn。一次路由只有一个归属。
4. 已运行子线程无需重复通知。已空闲且有新请求的子线程用宿主原生恢复机制续跑，并记录唤醒意图与序号，防止同一序号重复恢复。若任务、epoch 或压缩情况不明，暂停并核实未决工具效果，不能伪造旧上下文。
5. 暖路径由原子线程使用 `submit_action_and_wait_result` / `await_result` 保持。`pending` 仅表示没等到，继续同 action 观察；已提交效果不重发。父控制器不要插入每个暖回调。
6. 没有新工作时，父控制器用当前宿主支持的等待机制休息，醒后再次观察；等待本身不是 MCP 队列通知。观察间隔、连接状态和待核实项应明确。父线程停止后，当前包没有已验证的永久唤醒来源，这必须作为运行前提说明。
7. 连接失败时保留路由/收据/未决动作，重连后先读取状态，不自动重做。用户 Stop 或终止授权时停止接新工作，并通过真实宿主工具停止自己创建的任务；本机服务停止不证明云端任务已终止。
8. 容量不足时明确等待/报告，不合并路由或静默换模型。只有结清且用户确认结束的路由才关闭；最终文字回复不等于会话关闭。

这是一套主动父控制器可以执行的操作规则。当前包不附带宿主调度权限，也不应声称安装它就能永久无人值守。

## 尚需现场验收

- 正式父/子线程均实际看见新版八工具 schema，并能通过同一正式隧道读写。
- 用户空闲时 Desktop 发出新会话，真实父控制器观察并创建精确模型子任务。
- 同一 Desktop 会话多轮及真实本机工具回传，含等待超时、断线、不确定工具效果与权限确认。
- 实际平台长时间等待/暂停/重连后的父线程可用性和恢复行为。

这些现场项不能在用户 Mac 正处理其他工作时通过偷偷重启服务、改全局配置或创建新长期权限来代替验证。
