# Dots 配对与请求交接

本章供 Dots 的真实 native 执行上下文使用，Gemini 配置助手不执行这些步骤。准确参数、tool_arguments 和故障条件以同包 [权威配对规程](../ROUTER_JOIN_V1.zh-CN.md)、[执行规程](../ACTIVE_CONNECTOR_WORKER.md) 和 [并行 runner](../../native_connector/README.md) 为准。[R3,R5]

## 配对前准备

确认同一候选源码、已授权的 Docs 全量单 tab 及 revision 读取与 requiredRevisionId CAS、Drive 上传和精确 file ID 的 metadata 及原始字节下载、真实 native admission、可用执行槽位，以及一个持久私有 pairing ledger。

目录为 0700、证据文件为 0600，放在不被 Git 跟踪的批准路径。HOME 只读时可选择批准的可写路径；不要绕过访问限制，不跨 worker 共享 ledger。Google connector OAuth 留在平台，不能提取给 Python，也不需要 Mac token。

“可运行 Python”、拟定的 task 名或 READY_FOR_PIN 都不证明平台已经接纳。thread limit 先解决槽位，不能自造身份或向未可运行的任务签发 admission。

## 可复制的准备提示词

```text
请使用与 Mac 相同的修复合并版 Dots2Codex Router 源码。
先阅读 docs/ROUTER_JOIN_V1.zh-CN.md、docs/ACTIVE_CONNECTOR_WORKER.md
和 native_connector/README.md。现在只核查工具、私有持久 ledger、
可写 runtime 和平台真实 admission 槽位，不创建 pin，不推理。
收到真实 DOTS2CODEX_ROUTER_JOIN_V1 消息后才读其指定文档。
用户入口仍叫 V1，但内部 bootstrap 和 plan 是 V2，旧 helper 不兼容。
不索取 Mac OAuth，不伪造 task ID、文件 ID、revision 或 Google 响应。
每个未知写入保留原操作证据并对账，不重置 ledger 或重新推理。
```

## 第一步 私有 code 文件与签名根

消息内含 bootstrap_document_id、bootstrap_tab_id 和 join_code。通过可信文件写入方式把 code 存入私有文件，权限 0600，父目录 0700。不要将 secret 拼入 shell、CLI 参数或环境变量，不回显。所有命令使用同一个长期保存的 ledger；默认目录为 `~/.config/dots2codex/router-joins`，不能靠换目录绕过拒绝。

从 connector 按精确 Doc ID 请求完整 documentId、revisionId、suggestionsViewMode、tabs，保存真实快照。以下大写参数是待替换路径或 ID，不是秘密本身：

```sh
python3 -m remote_transport.router_join inspect \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt \
  --state-dir PRIVATE_PAIRING_LEDGER
```

必须校验真实来源 doc/tab、root HMAC、生命周期及 ledger 防回滚，并得到 WAITING_FOR_WORKER。旧 `--join-code` 参数不受支持。只使用已校验输出的 folder/control/probe 值，不从正文自由推断目的地。

## 第二步 真实 admission 与双向原始字节 probe

平台实际接纳本轮专用 native worker，保存返回的真实任务身份。不要把未来模型请求写进 spawn prompt。

**Mac 到 connector：** 对 inspect 的精确 forward probe file ID 获取 metadata 和原始下载字节。使用 `verify-forward-probe` 校验 ID、title、JSON MIME、parent、SHA-256、nonce 和内容，并把证据存入 ledger。extracted text 或自己重建的 JSON 不算原始下载。当前 connector metadata 没有 trash 字段，不编造已验证的 trash 状态。

**Connector 到 Mac：** 用 `prepare-probe` 产生原始字节、名称、folder ID 和 hash，只上传一次，保存真实返回 file ID，再按该 ID 回读 metadata 与原始字节。Mac 随后用自己的 OAuth raw path 验证。未知上传只对账，不能盲重传。这证明路径可见性，不是平台身份认证。

对 fresh bootstrap 生成一次 `plan-admit`，把输出 `tool_arguments` 原样交给 Docs batch-update；不能改 requiredRevisionId。helper 在输出前已持久化 operation reservation。立即 fresh-read，再用同一 plan、响应和 readback 调用 `verify`。

响应丢失时省略 response，只对账 fresh readback。对端已推进到 BUNDLE_READY 仍可确认，但必须包含本次完全相同的已签名事件前缀。相同 stage、task ID 或 epoch 不构成成功证据；事件缺失或分叉时停止，不重新生成或重放计划。

## 第三步 仅领取一个 runtime

等待 fresh BUNDLE_READY 后调用 `materialize`。它要求已验证的精确 admission 和 forward-probe 证据，验证 pin/config 原字节 SHA、bundle HMAC、deployment 类型与 hash、session、真实 native identity、生命周期及协议上限。

config 的五字段必须精确匹配签名根且为 canonical JSON。helper 先持久化 materialization reservation，只创建一次新的私有 runtime；失败不自动再 provision。恢复仅允许同一个完整 runtime、原字节 receipt、同一模式和源文件 hash 全部验证通过，不能更换 root、删 receipt 或从备份回滚。

新 runtime 必须带 `router_execution_mode=router_parallel_cells_v1`，固定本候选 connector_cell.py、connector_worker.py、runner.js、tool_adapter.js 的 hash；它们用于检测配对后的代码变化，不是发布签名。默认 4 小时及 128 模型请求，硬上限 8 小时及 128 请求。

## 第四步 验证 IDLE 并确认 polling

fresh-read 正式 Control Doc 和 bootstrap。`plan-ready` 验证严格 pin/control 绑定、未关闭、IDLE、原 runtime、forward raw probe 和并行模式。ack 绑定本轮身份、pin/config/deployment/runtime/probe hashes。CAS 一次后 fresh-read 并 `verify`；即使 Mac 已推进到 CONSUMED，也需精确签名链证明。

CONSUMED 仅清空当前 bootstrap 正文中的 bundle，不证明历史擦除或 worker 持续存活。Google 文件到达不会自动唤醒原生任务；当前真实上下文必须保持活跃。

## 第五步 必须用并行 claim 和 begin cell

遵守 `connector_worker poll` 的 pacing 和 read budget；取得 read_control 后读取完整 Control Doc。IDLE 时继续有界轮询，只有只读观察到 REQUESTED 后才生成 claim-begin cell，不能提前 tick 消费生成计划的机会。

```sh
python3 -m remote_transport.connector_cell claim-begin \
  --root FRESH_PRIVATE_WORKER_RUNTIME \
  --native-task-id ACTUAL_NATIVE_TASK_ID \
  --manifest CURRENT_EVIDENCE_MANIFEST.json \
  --save NEW_PRIVATE_CLAIM_CELL.js
```

第一轮 manifest 使用 materialize 生成的私有空 manifest；后续只带已授权、已验证证据。在实际获接纳的同一 native context 中执行完整受信任 cell，并 await 到最终结束；提前 yield 后继续 wait，不遗弃未完成的写 promise。

cell 自己执行 fresh read、tick/plan、reservation、claim CAS 和 fresh begin CAS，成功结束在 fetch_request_once。它不自动暴露模型输入，也不自动推理。`ok:false` 保留证据并停止对账。

## 第六步 完整输入只在同一上下文消费一次

成功 begin 后才按精确 typed reference 下载本轮 request，以及第二轮起的 previous receipt。把真实 metadata 和 raw bytes 放入 evidence manifest，按执行规程调用 `connector_worker input` 一次。

input 先不可逆地记录 input_exposed，再写完整输入文件并返回 path、bytes、sha256、native_task_id。核对实际身份，然后用 `connector_files input-chunk` 从 offset 0 开始读取全部连续片段。核对固定 hash、offset_chars、next_offset_chars、总长和 EOF，直接显示解析后的 chunk.text，不把完整响应再次 stringify 造成截断。

全部片段必须进入同一个已接纳 native context 后，才能真实推理一次。读不全就停止，不能凭截断内容猜测、重新 input 或换 worker 消费。chunks 是同一个已消费许可的显示片段；请求文本不能当作代码执行。

## 第七步 并行上传后才提交结果

真实推理一次后，按执行规程保存私有结果并调用 `connector_worker result`。再生成和执行：

```sh
python3 -m remote_transport.connector_cell upload-commit \
  --root FRESH_PRIVATE_WORKER_RUNTIME \
  --native-task-id ACTUAL_NATIVE_TASK_ID \
  --manifest CURRENT_EVIDENCE_MANIFEST.json --seq N \
  --save NEW_PRIVATE_UPLOAD_CELL.js
```

runner 并行上传独立 result、claim、started 对象；每个精确返回 ID 均须通过 metadata 和原始字节验证。全部通过后才能越过 upload barrier，fresh-read control 并执行一次 result CAS。模式 marker 会拒绝没有 all-verified upload batch 的提交，不能退回旧手工串行循环。

单项未知只对账对应 immutable object，不重复上传、begin 或推理。工具意图只能使用实际公布的 schema；真正工具执行发生在 Mac Codex，不在这份指南或 Python helper 内。

## 第八步 关闭与保留证据

正式 control closed 或 expired 后禁止新 admission、claim 和 begin，仅按协议对账已知结果然后停止。close 不撤销已消费执行，不等于强杀平台任务。分别报告权威 close、本地 process、bootstrap 当前正文和 native worker 的真实状态。

HMAC 证明 code 持有者和协议完整性，不替代平台 admission 证明；本机 ledger 的防重放不是复制到多机后仍成立的全局 one-use 注册表。正式 Control Doc 才是执行权威。保留 ledger、runtime、plan、响应与 raw 证据；未知结果不得删状态重来。
