# 全局 Direct：原生子线程控制端

本页仅适用于一键启动生成的 `mode: global` 配置。旧的单路 CLI 试验步骤不适用。Mac 一键入口负责启动隧道、MCP stdio 和本机 Responses 服务；控制端仍需使用真实原生子线程接单。

## 先确认实际工具可用

原有八个工具名保持不变，另增原生 web 专用 `prepare_hosted_call` 和 `record_hosted_result`（共十个）：`bridge_status`、`get_request`、`discover_tools`、`lookup_schema`、`submit_action_and_wait_result`、`await_result`、`finish_request`、`cancel_request`。全局版增加了路由归属字段，首次换版必须同步插件工具定义，并确认新创建的真实子线程能看到全局版参数。旧子线程缓存的旧 schema 不算通过。

`bridge_status({})` 应显示 `mode: global`、本次 `instance_id`、持久 `config_id`、`allowed_pairs`、`max_routes` 和 `pending_routes`。`actual_native_platform_verified: false` 是诚实边界：Python 仅记录可信单用户提供的逻辑归属，不证明平台身份或实际模型。父线程必须依据真实平台创建结果记录子线程。

## 为每个会话创建一次真实子线程

1. Codex 发来请求后，`pending_routes` 出现该路由的 `route_id`、`model` 和 `reasoning_effort`
2. 父线程用平台原生创建工具，显式采用该模型/推理强度，创建一个实际子线程。默认是 `gpt-6-astra` / `xhigh`。不能静默降级，也不能由 Python、API 推理或其他替代实现接管
3. 把该路由和它自己的真实任务 ID 交给子线程。子线程以稳定的 `context_epoch` 首次调用：

```json
{
  "route_id": "bridge_status 返回的路由",
  "worker_id": "实际原生任务 ID",
  "context_epoch": "为该原生上下文保留的唯一标识",
  "model": "gpt-6-astra",
  "reasoning_effort": "xhigh",
  "after_seq": 0,
  "wait_ms": 5000
}
```

这是 `get_request` 的参数。首次返回包括 `claim_token`、`request_id`、`seq`、`context_token` 和上下文；保留它们。后续 `get_request` 使用 `route_id`、`claim_token`、`after_seq`、`wait_ms`。另一个子线程或另一上下文不能重新认领该路由。同一个实际子线程不能绑定多个路由。

`replayed: true` 表示同一请求回执重读，绝不表示新的执行许可。若该请求已在处理，继续原来的工作。

## 处理工具动作和最终回答

- 首轮 `context.kind` 是 `full`；同一原生上下文后续通常是 `delta`。完整源请求留在本机 SQLite。增量含新增用户消息或工具输出，未经裁剪的其他字段变更，以及工具目录变化
- 动态工具定义按需读取。`discover_tools` 需要 `route_id`、`claim_token`、`request_id`、`context_token`、`query`；然后 `lookup_schema` 传相同归属字段，令 `name = matches[i].key`（完整 JSON 工具键，不是简单工具名称）、`sha256 = matches[i].schema_sha256`，取得 `schema_token`
- 需要本机工具动作时，调用 `submit_action_and_wait_result`，传上述四个归属字段、唯一且稳定的 `action_id`、完整 Responses `response`、本次读取的 `schema_tokens` 数组，以及 `wait_ms`
- 原生子线程只返回工具意图；Codex 客户端在自己的权限和确认流程内执行。Python 不执行本机工具，也不改变批准策略
- 超时返回 `pending` 只是观察结果。用 `await_result` 继续等，传 `route_id`、`claim_token`、先前的 `request_id` 和同一个 `action_id`。不要新建子线程，不要换 action ID 重做
- 最终文字回答使用 `finish_request`，参数为四个归属字段、`action_id`、`response`、`schema_tokens`。后续用户继续同一会话时，仍用同一子线程及新的 `after_seq`
- 能明确识别为 Direct 来源的传输工具会从动态工具目录中隐藏，并禁止作为客户端工具意图发回，避免递归调用自身。包括实际连接器的 `mcp__codex_apps__dots2codex_direct_*` 命名，以及明确的 Direct namespace。单独的 `get_request` 或其他产品的同名工具不能据此断定来源，保持普通客户端工具语义；若客户端移除了来源信息，需先确认工具归属，不能把本 MCP 传输工具当本机动作递归调用

## 等待、关闭和容量

Stop 关闭本机网关和隧道，拒绝新的请求、认领和响应发射，并尝试持久记录已认领路由的停止请求；原有去重记录保留。磁盘写入失败也不会阻止本机服务停止。它不能证明云端原生子线程已被中断；`native_children_stop_confirmed: false` 会明确保留这一边界。父线程应在用户停止或连接丢失后，使用真实平台工具中断自己创建的子线程；需要继续时恢复原来的子线程。已发出的本机工具效果仍需检查，不能因为本机 PID 消失就视为撤销。

服务没有后台推理、心跳、自动创建或自动唤醒原生子线程。`automatic_wake: false` 是设计事实。父线程仍需保持合适的观察等待；已有子线程空闲时，通过平台原生消息机制恢复同一个子线程。不可把 MCP 长轮询超时解释成自动唤醒已配置。

默认最多 8 条同时打开的路由，每条最多 128 个模型请求；实际平台并发额度可能更小。控制端应按实际额度接单，不能假称还有可用子线程。

完成最终回答不会自动关闭会话，因为用户可能继续提问。确认某个会话已结束时，让它自己的子线程调用：

```json
{"route_id":"原路由","claim_token":"原认领回执","close_route":true}
```

这是 `cancel_request` 的参数。只允许关闭已完成最终文字回答或已取消的路由；已提交工具调用、尚未收到实际回传的路由仍未结清，拒绝关闭。关闭后立即释放活动容量，保留历史去重记录。关闭后旧会话不能重新分配给别的子线程；开始新 Codex 会话会产生新路由。容量用尽时明确返回 `route_capacity_exhausted`，不静默串线或回收仍在用的会话。

取消尚未提交结果的请求：`cancel_request` 传 `route_id`、`claim_token`、`request_id`。取消不能撤销已发给客户端的效果。取消后的路由可以关闭；要继续工作，使用新会话。

## 重启与不确定结果

- 正常服务重启不会清空请求、action ID、认领回执、上下文回执或工具结果发射记录。原生子线程上下文仍在时，同一个子线程继续使用原回执
- 若原生上下文本身丢失、发生未确认的压缩或任务被替换，不得伪称 epoch 未变化来继续 delta；暂停该路由，确认未决效果，关闭后开始新会话
- 普通纯文字结果可安全重取，不产生第二次原生提交
- 含工具意图的响应在写 socket 前有持久发射栅栏。断线后重复 POST 会得到 `delivery_outcome_unknown_no_reemission`，不重发可能已执行的工具。用同身份的 `GET /v1/responses/<request_id>` 检查状态；此接口不重发工具内容。检查客户端已有输出后再决定下一步，不能自动重做

## 客户端边界与验收

本机接口要求私有 bearer；Desktop 和 CLI 通过各自读取的全局 provider `http_headers.Authorization` 提供本机 token，不依赖 Desktop 继承 Terminal 环境。隧道控制面 key 永不写入 Codex provider。

路由依据 Codex 提供的标准 `session-id` 和 `thread-id`，同一会话固定模型/推理强度。缺失、重复、下划线旧式身份字段、未经批准的模型组合都会拒绝，不猜测项目路径或混用队列。

自动化测试运行真实 localhost HTTP 和官方 SDK MCP stdio，并用合成客户端/工作线程验证并发、多轮、重启、取消、去重、增量和 schema。它们不证明实际 Mac Desktop、真实隧道或原生子线程已经跑通。最终现场验收需单独记录实际工具同步、真实子线程接单和 Codex 本机工具回传。

## 有界云端原生验证

独立的云端夹具已验证实际原生任务参与的三轮连续处理、空闲后恢复、MCP/桥接进程重启、同一归属与重复提交去重。控制工具入口使用私有测试邮箱替代，Codex HTTP 客户端为合成；这不是正式隧道或 Mac Desktop 验收，也没有新增永久调度器。`automatic_wake` 仍为 false。

复现步骤、实际父控制器循环规则及未验证项见 [云端无人值守验证](UNATTENDED_CLOUD_VALIDATION.zh-CN.md)。去敏后的原生创建/恢复与桥接计数摘要见 `orchestration/unattended_native_result.json`；普通回归测试中的合成工作线程证据与它分开记录。

## 工具协议 v2：完整客户端声明与原生 web

全局/单次会话服务现有工具协议为 `dots-direct-tools/2`，MCP 0.3.0。
原有八个工具保留，另增 `prepare_hosted_call`、`record_hosted_result`；需刷新
已安装插件工具定义。真实客户端的 function、custom、namespace、client
`tool_search` 和 web_search 声明均可进入动态目录。不要把 web/search 当作普通
无名称函数，也不要为了纯文字请求而删除客户端默认 cached web 声明。

先读取 `get_request.capabilities`。默认 cached（`external_web_access:false`）、
indexed、位置、检索上下文大小等选项，原生 web 工具目前无法精确实现；只有
尝试执行时明确报能力错误。未经用户明确选择，不得改为 live，不得改设置。

原生 web 必须先读取精确 schema，再用稳定 operation_id 调用 prepare，只有
首次 `execute:true` 才按返回的完整参数调用一次实际可用的原生 `web.run`。
用 record 回传完整原生结果及真实 URL/引用，最终响应包含返回的已完成
web_search_call。它是已发生的原生操作，不是等待 Mac 执行的意图。
重读、超时、断线或重启不能授权重新执行。回执只是可信工作线程的报告，
不是平台执行证明。详细参数与边界见 [工具兼容说明](TOOL_COMPATIBILITY.md)。

client tool_search 必须发出真正的 tool_search_call（arguments 为对象），等待
实际工具搜索回调；回调中的 namespace/function 定义才能作为新工具读取。
不要臆造工具或改成普通函数。默认模型目录现已开启 client search-tool 能力。

图片通过真实 MCP ImageContent 块交付。若在 functions.exec 里包装 MCP 调用，
必须逐一 `image(block)` 转发返回的图片块，并单独转发文本；只打印 JSON
或 base64 不算原生模型看到了图。完整来源和精确工具回调仍保留。当前为
单张 512 KiB、1600 万像素、完整历史最多 8 张且完整请求最多 1 MiB；超过
界限会明确报错，不会悄悄缩图。只接收验证过的 data URI 图片，不自动下载 URL。

网页答案应在正文写可打开的真实来源链接，不能只靠 annotations；固定客户端
会丢弃这些注释。内部 turn 引用不能当成客户端能用的链接。此前同一路由中
已经验证的来源可继续引用，无需为引用而重复检索。
