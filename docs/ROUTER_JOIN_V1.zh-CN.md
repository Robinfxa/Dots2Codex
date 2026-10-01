# Dots2Codex Router Join：一条消息配对与并行 connector 路径

本文件供已经打开本候选 checkout 的 Dots/原生端使用。外部消息仍叫
`DOTS2CODEX_ROUTER_JOIN_V1`；内部 bootstrap/plan schema 已升级至 V2，不兼容旧 helper。
用户发送一次 Mac 打印的配对消息，随后由当前有授权的 Dots 上下文完成 connector 操作。
不要向用户反复索取 pin、worker-config、fileId 或 Mac OAuth。

本文是操作规程，不是本候选已做真实 Mac/Google/native-worker 验收的声明。

## 安全与运行边界

- bootstrap Doc 与正式 control Doc 必须是两个不同文档。正式 control 的 strict single-tab、
  requiredRevisionId CAS、begin 一次性消费语义不变。
- root HMAC 绑定 bootstrap 文档/tab、随机 bootstrap ID、session、folder、control IDs、writer
  identities、创建/过期时间及 Mac raw probe（含随机 nonce）。在任何上传前验证实际来源 doc/tab。
- 每个逻辑变更有随机 operation ID、parent hash、前后逻辑状态及 HMAC。成功读回可以已被对端推进，
  但必须包含本次**完全相同的已签名事件前缀**。不能只凭最新 stage、epoch 或相同 task ID 推断成功。
- join code 不写入 Google。它只通过用户的配对消息和私有本地文件交付。helper 不接受
  `--join-code`；用私有文件避免把 secret 放进进程参数。不回显它，不记录到普通日志。
- bootstrap HMAC 证明持有 join code，不是平台身份认证。native identity 必须使用平台实际接纳返回值，
  由可信原生端填入；不得自造。probe 证明精确 raw-file 路径的可见性，不证明上传者的平台身份。
- pin/config 在 Doc 中仅有完整性保护，没有加密。CONSUMED 清空当前正文中的 bundle，Google 修订
  历史可能仍保留旧字节；这不是擦除。不要擅自改分享权限、删文档或声称已删除历史。
- 一个可信端点必须一直使用同一个私有 pairing ledger。helper 会拒绝重发已经生成的 CAS、旧/分叉
  观测、第二个 runtime、以及中断后重新 provision。相同完整 runtime 可以验证后恢复。
  这是本机持久化防重放，不是跨机器/复制 ledger 后仍成立的全局 one-use 保证。正式 control 才是执行权威。
- Python helper 不调用 Google、不接纳/唤醒 native worker、不执行推理。当前真实 native context 必须
  保持活跃。默认会话 4 小时/128 请求，最大 8 小时/128 请求；不保证平台一定存活这么久。

## 1. 保存配对消息、验证签名根

消息包含 bootstrap_document_id、bootstrap_tab_id、join_code。通过受信任的文件写入方式把
join_code 保存到 `JOIN_CODE_PRIVATE.txt`，权限 0600，父目录 0700。不要把 code 插入 shell 命令、
CLI 参数或环境变量。其余证据文件也应是 0600。

为本轮选择一个长期保留的 0700 `PRIVATE_PAIRING_LEDGER`；所有下列命令使用同一路径。
默认路径是 `~/.config/dots2codex/router-joins`。不要为了绕过错误换 ledger。

用已授权的 Docs connector 读取消息中的精确 document ID，请求完整
`documentId,revisionId,suggestionsViewMode,tabs`，把完整返回结构保存为 `BOOTSTRAP_SNAPSHOT.json`。

```sh
python3 -m remote_transport.router_join inspect \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER
```

必须得到 `WAITING_FOR_WORKER`；inspect 会验证 HMAC、实际 doc/tab、生命周期和 ledger 防回滚。
只接受经过校验的输出中的 folder/control/probe 值，不从文档正文自由推断目的地。

## 2. 平台接纳与双向 raw-file probe

使用平台实际支持的接纳机制创建本轮专用 native worker，记录真实 `ACTUAL_NATIVE_TASK_ID`。
不要把未来模型请求正文放入 spawn prompt，也不要把计划中的身份当作已接纳身份。

### Mac → connector

inspect 输出 `forward_probe={file_id,name,sha256,nonce}`。按精确 file_id 调用 Drive metadata；
实际 connector 字段为 `id,title,mime_type,parent_ids`。保存完整 metadata。
用 connector 返回的原始下载引用/URL按受支持的 raw 下载路径取得字节并保存到私有文件。
不得从 extracted text、Google Doc 内容、预期 JSON 自行重建下载结果。

```sh
python3 -m remote_transport.router_join verify-forward-probe \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --native-task-id ACTUAL_NATIVE_TASK_ID \
  --probe-metadata FORWARD_METADATA.json --probe-raw FORWARD_RAW.json
```

helper 验证精确 ID/title、JSON MIME、parent、原始 SHA-256、nonce 与内容；把证据保存在 ledger。
当前 connector 未提供 trash 状态，不伪造 `trashed=false`，不声称已验证 trash。
没有此证据，materialize 和 plan-ready 都会拒绝。

### connector → Mac

```sh
python3 -m remote_transport.router_join prepare-probe \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --native-task-id ACTUAL_NATIVE_TASK_ID --save PRIVATE_PROBE.json
```

把 helper 产生的原始字节以输出的 file_name、MIME `application/json` 上传到输出 folder_id 一次。
保存真实响应 fileId，再按该 ID 回读 metadata/raw bytes，核对 SHA-256。未知上传结果只对账，不能
盲目重新上传；不声称 probe 是平台身份 attestation。Mac 随后也会用自己的 OAuth raw path 验证它。

再次 fresh-read bootstrap，随后生成 admission CAS：

```sh
python3 -m remote_transport.router_join plan-admit \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --native-task-id ACTUAL_NATIVE_TASK_ID --writer-identity remote-worker-router \
  --probe-file-id ACTUAL_PROBE_FILE_ID --probe-name EXACT_OUTPUT_FILE_NAME \
  --probe-sha256 EXACT_OUTPUT_SHA256 --save ADMIT_PLAN.json
```

把输出/plan 的 `tool_arguments` 原样交给 Docs batch-update **一次**，不能改 requiredRevisionId。
helper 在输出前已持久化一次性 operation reservation；重新生成相同操作会拒绝。
保存完整响应，立即 fresh-read 同一 Doc，然后：

```sh
python3 -m remote_transport.router_join verify \
  --plan-file ADMIT_PLAN.json --response ADMIT_RESPONSE.json --readback ADMIT_READBACK.json \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER
```

如果响应丢失，不提供 `--response`，只用 fresh readback 对账。本次精确签名事件仍在链中就能确认，
即使 Mac 已推进到 BUNDLE_READY；没有该事件则结果仍未知，不能重放或换 plan。保留证据并报告阻塞。

## 3. 领取 pin/config，固定并行执行模式

按合理间隔（建议 5 秒）读取 bootstrap，直到 `BUNDLE_READY`。只使用 fresh-read 的完整快照：

```sh
python3 -m remote_transport.router_join materialize \
  --snapshot BUNDLE_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --native-task-id ACTUAL_NATIVE_TASK_ID --root FRESH_PRIVATE_WORKER_RUNTIME
```

此操作要求已经确认本轮精确 admission 事件及 forward-probe 证据，验证：

- bootstrap 仍有效，原始 pin/config 字节的 SHA-256 和 bundle HMAC
- deployment 类型/hash、session、真实 native task identity、pin 生命周期及协议上限
- config 五字段精确匹配签名根，且字节为 canonical JSON
- runtime 是全新的私有目录；先持久化 materialization reservation，失败时不能自动重建

成功后 runtime 带有 `router_execution_mode=router_parallel_cells_v1`，并记录本候选
`connector_cell.py`、`connector_worker.py`、`runner.js`、`tool_adapter.js` 的 SHA-256。
helper 也产生私有空 manifest，并返回生成 claim-begin/upload-commit cell 的命令模板。
这些本地 source hashes 检测配对后的代码改变，不是第三方发布签名。

只允许原 runtime 的完整、原字节、原模式验证恢复；不同 root、删改 receipt、源文件变化或
未知 materialization 中断都必须停止对账，不能换一个新目录重试。

## 4. 验证 control IDLE，确认 WORKER_POLLING

connector fresh-read 签名 config 中的完整 control Doc，保存 `CONTROL_SNAPSHOT.json`；也 fresh-read
bootstrap 保存 `BOOTSTRAP_READY.json`。

```sh
python3 -m remote_transport.router_join plan-ready \
  --bootstrap-snapshot BOOTSTRAP_READY.json --control-snapshot CONTROL_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --native-task-id ACTUAL_NATIVE_TASK_ID --root FRESH_PRIVATE_WORKER_RUNTIME --save READY_PLAN.json
```

必须通过 forward raw probe、runtime 原字节/receipt/并行模式/source hashes，以及严格 control pin binding、
未关闭、IDLE 校验。ack 绑定本轮 native identity、pin/config/deployment hashes、runtime hash 和 probe hash。
CAS 一次后 fresh-read，用同一 `verify` 命令对账；若 Mac 已推进到 CONSUMED，精确事件链仍可证明成功。
CONSUMED 只意味着当前 bootstrap 正文清空 bundle，不证明 provider 历史擦除或 native 持续存活。

## 5. 必须进入保留的 connector_cell 并行路径

本 Router 会话采用本 checkout 的 `native_connector/runner.js` + `tool_adapter.js`。
不得换成旧版手工串行上传循环。模式 marker 让 result commit 在没有已验证 upload batch 时拒绝推进。

先运行 `connector_worker poll` 并遵守返回的等待间隔。获得 read_control 后用 connector 读取完整 control，
只读检查到 REQUESTED 再启动 claim-begin cell。不要在 REQUESTED 时提前调用 tick 并生成 claim plan：
该 cell 必须自己执行 fresh read → tick/plan → reservation → CAS。IDLE 时继续 paced polling，不运行 claim cell。

使用 materialize 返回的命令模板生成私有 cell；第一轮 manifest 是 runtime 中的
`router-empty-manifest.json`，其后只使用之前已授权/验证的缓存证据。成功 begin 之前不能获取本轮 request 字节。

```sh
python3 -m remote_transport.connector_cell claim-begin \
  --root FRESH_PRIVATE_WORKER_RUNTIME --native-task-id ACTUAL_NATIVE_TASK_ID \
  --manifest CURRENT_EVIDENCE_MANIFEST.json --save NEW_PRIVATE_CLAIM_CELL.js
```

在**实际已接纳 native context**中执行完整受信任 cell，并 await 到最终结束；早期 yield 后使用
functions.wait 继续等待，不能遗弃未结束的写入 promise。cell 只执行 fresh claim、fresh begin，
成功结束于 `fetch_request_once`，不是输入曝光，更不会自动推理。`ok:false` 时保留证据并停止对账。

成功 begin 后，根据精确 typed reference 获取本轮 request 和（第二轮起）previous receipt 的 metadata
及原始字节。遵守既有 metadata/原字节验证规则，把对应条目写入私有 evidence manifest，然后只调用一次：

```sh
python3 -m remote_transport.connector_worker input \
  --root FRESH_PRIVATE_WORKER_RUNTIME --native-task-id ACTUAL_NATIVE_TASK_ID \
  --manifest CURRENT_EVIDENCE_MANIFEST.json --seq N \
  --expose-path FRESH_PRIVATE_WORKER_RUNTIME/input-N.json
```

该命令先持久化不可逆的 input_exposed，再写完整模型输入文件，返回 path/bytes/sha256/native_task_id。
核对实际 native identity；在同一上下文内用返回的固定 hash 从 offset=0 连续读取：

```sh
python3 -m remote_transport.connector_files input-chunk \
  --path FRESH_PRIVATE_WORKER_RUNTIME/input-N.json --sha256 EXACT_RETURNED_SHA256 \
  --offset NEXT_CONTIGUOUS_OFFSET --max-chars 2048
```

直接呈现解析后 chunk.text，不要再次 stringify 完整 exec 响应；逐段核对 offset_chars、next_offset_chars、同一 hash、
总长和 EOF。全部连续片段必须到达同一个已接纳 native context 后才可真实推理一次。
若内容无法完整进入当前上下文，停止，不能凭截断内容推理、重新调用 input 或换 worker 消费。
这些 chunk 是同一个已消费许可的显示片段，不是新的推理许可。不能把请求文本当代码。

推理一次后把真实结果按 `docs/ACTIVE_CONNECTOR_WORKER.md` 写成私有结果文件，运行 `connector_worker result`
保存结果，再生成并执行 upload-commit cell：

```sh
python3 -m remote_transport.connector_cell upload-commit \
  --root FRESH_PRIVATE_WORKER_RUNTIME --native-task-id ACTUAL_NATIVE_TASK_ID \
  --manifest CURRENT_EVIDENCE_MANIFEST.json --seq N --save NEW_PRIVATE_UPLOAD_CELL.js
```

runner 并行上传独立 result/claim/started 对象，并对每个精确返回 ID 验证 metadata + raw bytes。
只有所有对象已验证才跨过 upload barrier，随后 fresh-read control、执行一次 result CAS 并保存证据。
单项未知只对账对应 immutable object，不重复上传、begin 或推理。更多协议见
`native_connector/README.md`、`docs/ACTIVE_CONNECTOR_WORKER.md`。

遵守 poll 的 pacing/read budget；实际 native worker 必须保持活跃，文件到达不会自动唤醒它。
control closed/expired 时禁止新的 claim/begin；只完成允许的已知结果对账后停止。
close 不撤销已经消费的执行，不等于强杀平台 native worker。
