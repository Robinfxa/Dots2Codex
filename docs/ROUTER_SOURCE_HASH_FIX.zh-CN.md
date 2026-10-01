# Router source-hash 合约修复与旧会话恢复边界

## 故障与根因

模型选择版本在真实 Router 验收中完成了配对，但首个 `REQUESTED` 到达后，
`connector_cell claim-begin` 在本地生成阶段报 `router_parallel_runtime_source_changed`。
这个失败发生在生成的 JavaScript 执行之前；该生成器本身不调用 connector、CAS 或模型。

原因是生产者 `router_join._source_hashes()` 已记录六个源码文件，而消费者
`connector_cell` 仍维护独立的四个文件精确白名单。于是新 materialization 即使所有
文件内容完全一致，也会被拒绝。这个问题同时影响新版本创建的 selected 和 legacy Router，
并不限于某个模型、effort 或 Google 权限。

早先回归只有“篡改后应拒绝”的 cell 测试，没有把成功 materialize 的输出继续交给
真实 cell 生成器。它未覆盖这个模块交界，不能用此前离线通过来否认真实故障。

## 最小修复

只改一个生产文件：`remote_transport/connector_cell.py`。
它直接调用 materializer 使用的同一个 `_source_hashes()`，把保存的字典与当前完整
字典精确比较，不再复制一份可能过期的文件列表。

以下六个字段都保留，且每项仍核验实际文件字节：

- `remote_transport/connector_cell.py`
- `remote_transport/connector_worker.py`
- `native_connector/runner.js`
- `native_connector/tool_adapter.js`
- `remote_transport/selection.py`
- `remote_transport/native_capabilities.json`

缺少或增加键、改 digest、改任意受绑定源码、删去两个模型绑定项以恢复四键列表，
都会失败关闭。共享生产者还检查源码为存在的非符号链接文件，长度在原限制内。
没有跳过验证、serial fallback、pin 重新计算或自动重试。

## 旧 runtime 不能原地继续

协议形状和 `router_parallel_cells_v1` 标记不变；这是同一六文件合约的消费端修复。
但 `connector_cell.py` 自身就在六个被固定的文件中。修复后它的 digest 改变，
旧 runtime 的 `worker.json`、配对 ledger 和 `router-materialization.json` 仍固定旧 digest。
因此更换代码后旧 runtime 被拒绝是正确行为，不能修改这些记录来“修复”匹配。

本版本没有迁移、重新签署或替换已有 materialization 的流程。**使用新配对会话。**
不能只凭 REQUESTED 或一个本地错误猜测整个会话从未执行；关闭和恢复前仍须核对正式
Control、对应操作回执及 worker 记录，尤其检查是否存在已消费 begin 或未知工具结果。

建议恢复顺序（本次源码修复没有执行这些外部动作）：

1. 暂停旧客户端的新请求，保留旧 checkout、pin、journal、配对 ledger 和精确错误证据
2. 用原有授权流程核对正式 Control；关闭旧会话并分别核验 Control 已关闭、Mac facade
   进程已停止，以及原生 worker 是否真的停止。不要把关闭 CAS 当作原生停止确认
3. 两端使用同一完整修复版本，在新私有 runtime、新 session ID、新 bootstrap、
   新 native admission 和新 pin 下重新配对。不要修改旧 hash、删除 journal 或复用旧 permit
4. 在新会话先确认 materialization 输出的六项与 cell 生成器一致，再进行一次已授权的
   最小请求与工具续轮验收。生成成功不等于 generated cell 已执行，更不等于模型推理已完成
5. 如果旧会话中已有未知执行或工具结果，先按原恢复规程对账；不要把旧请求自动搬到新会话重放

## 回归验证

`remote_tests/test_router_cell_materialization.py` 使用真实 helper 完成离线
plan-native / record-native（仅假 admission）、probe、admit、bundle 和 materialize，
然后以独立 Python 进程调用 cell CLI。测试覆盖：

- selected `gpt-6-astra/max` 和 legacy 的 materialize → claim-begin / upload-commit 生成成功
- 全部六个保存 digest 的篡改，在两种模式和两个阶段均拒绝
- 六个实际源码文件分别变化时拒绝；测试只改临时副本
- 缺键、多键、退回四键、旧版已固定 cell digest 均拒绝
- 失败不输出 cell、不改 worker 状态；成功生成也不消耗 CAS 或 native input

同一正向集在修复前稳定复现四次失败，修复后通过。它只生成 JS，不执行 connector
或 native inference。独立审查已通过 selected V3 与 legacy V2 两条完整 generated-cell → 假 connector
流程，均到达 `result_committed`，并通过 64/64 个 source-contract 故障检查。每条流程
执行 122 次真实本地 helper 命令、3 次假 CAS 和 3 次假上传；input 重放均被拒绝。
真实新会话验收必须另行记录，不能由这些离线测试替代。
