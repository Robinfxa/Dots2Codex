# 安全边界与验收范围

本指南 G2 把 Google Cloud 与 Gemini 配置说明迁移到 2026-10-01 修复版 Router，保留新版长会话、工具续轮、并行 cells、输入分块及无正文 timing。Gemini 没有被接入模型后端，指南与可选 helper 不自动授予任何云权限。

## 版本和证据等级

迁移基线源树为 `8da5c5ada6c3fa85dc64317653f5f7961b8adf4eed73ae9a44563e8141a4e5c8`，以发布提交 `689efa055e6bb2d290f8870e25ff1ebb3a3109bd` 为底合并修复。新增指南不能带回旧 Router 代码。最终增强包的树 hash 和 ZIP hash 由包清单及外部校验文件给出，不能使用旧 v1 ZIP hash 安装。

**历史现场基线：** 官方 Codex 0.159.2 的两个依赖文本回合、一次真实 Mac 工作区工具循环、四个模型请求及最终 receipt/显式 close 已有先前记录。这发生在新 Router 自动化配对之前，不能替代新链路验收。

**修复版离线基线：** `docs/ROUTER_VALIDATION.md` 记录 441 次 Python 测试和 25 个 Node 测试，包括虚拟 provider 和本地合成子进程。该数字属于修复基线，不自动代表本次增强包的重新执行结果。

**本次迁移离线验证：** 随包 `verification/router-tests.txt`、`verification/setup-helper-tests.txt` 及发布验证摘要记录实际命令与结果。helper 使用虚拟凭据与 mock 网络；实际安装的 OAuth SDK 1.5.0 在 mock 环境运行，requirements 固定的 1.2.2 仅核对官方源码兼容性，不能声称本轮真实安装或运行过 1.2.2。

**仍待现场验收：** 新 Router 单消息配对、真实双向 raw-file 访问、Mac 启停故障路径、实际 native admission 与完整输入、真实并行延迟，以及多小时运行。没有本轮 Google 云写、OAuth consent、真实模型请求或远端发布。

默认 4 小时及 128 请求、硬上限 8 小时及 128 请求是协议边界。合成 128 请求测试、配置 TTL 和历史四请求成功均不证明实际多小时稳定性。平台不会因 Drive 文件到达自动唤醒 worker。

## OAuth 与资源权限

`drive.readonly` 是受限的广泛只读范围，可查看和下载授权账号可访问的 Drive 文件；不是“只有 transport folder 可读”。指定 folder 的应用约束不会缩窄 OAuth grant。若拒绝该范围，应暂停并设计替代授权路径，不删除代码检查。[G7]

Cloud IAM、OAuth scopes、Drive ACL 和平台 connector 是不同边界。同一 Google 用户不等于同一 OAuth app；共享文件夹也不等于所有跨应用读取都已可用。不自动扩完整 drive 读写、不全域委派、不公开分享。[G7]

External Testing 下，本指南请求的 Drive scopes 不属于仅基本登录资料的例外；授权与 offline refresh token 在 consent 后七天到期。Production 切换不能保证永久有效或自动免审核。具体发布、验证与安全评估要求应按实际用途核对。[G6,G7]

## 配对秘密与历史保留

启动器有意把私有 join 消息显示在 Mac Terminal 并复制到剪贴板；本地 join-message.txt、private-pairing.json 和用户发给 Dots 的消息也可能保留 code。它不写入 Google bootstrap，但这不代表没有本地或聊天副本。不要发给 Gemini，发送后清空剪贴板并保护终端与本地记录。

Dots 仅从 0600 私有文件通过 `--join-code-file` 读取；父目录为 0700。HMAC 证明 code 持有者和协议完整性，不是加密、发布签名或平台身份认证。真实 native identity 必须来自平台实际 admission。

pin/config 以 base64 保存，授权阅读者可读。CONSUMED 清空当前 bootstrap 正文的 bundle 字段，不能证明 Google 修订历史、备份或下载副本已删。本包不实施历史删除，也不声称历史永远不可删除；额外删除或保留策略需另行审批，且不能损害仍需保留的证据。[G9]

## 持久化防重放与一次性输入

签名根绑定真实 bootstrap 文档和 tab、nonce、session、folder、独立 Control Doc、writer identities、时间及 forward probe。每次逻辑转换有随机 operation ID、parent hash、前后状态和 HMAC。

同一持久 ledger 记录观测、一次性计划、材料领取 reservation 和验证结果。响应丢失或对端提前推进时，仅本次精确签名事件前缀可以证明成功；stage、epoch 或相同 task ID 不够。未知写入不重放，未知 materialize 不新建第二个 runtime。

ledger 是本机持久化保护，不是多机复制后仍成立的全局 one-use 保证。正式 Control Doc 的 strict single-tab、requiredRevisionId CAS、一次性 begin/input 和既有工具重放保护继续决定执行权。

成功 begin 后才取本轮请求。input_exposed 先落盘，再完整分块传入同一个真实 native context；读不全不能推理、重新 input 或换 worker。并行 upload-commit 必须验证每个对象的精确 ID、metadata 与 raw bytes，全部通过后才提交结果，不退回旧串行路径。

## 权威关闭和进程停止

修复版 `STOP_ROUTER.command` 先持久化 stop intent，等待启动临界区，直接操作正式 Control Doc；不依赖 facade 在线或 ready 文件存在。未知或不可读的权威状态进入 RECOVERY_REQUIRED，不报告 CLOSED。

权威 close、process_stopped、bootstrap_cleanup 与 worker_stop_confirmed 分开报告。已核验 close 阻止未来 admission、claim 和 begin，但不撤销已消费执行，不等于强杀 native worker。没有平台停止回执就明确未确认。

启动子进程需持久 PID 与 OS 身份 grant；不能凭重用 PID 或进程同名发送信号。OS、磁盘或网络硬故障可能仍需人工核对。未知 close 只读对账原 operation ID；明确 CAS 拒绝才允许下一次显式 stop 重新规划。

## 本地 Codex 和 Gemini 边界

本包保留 on-request approval、workspace-write 及既有 provider/retry 设置。没有独立 CODEX_HOME 或自动忽略全局配置的保证；审查工作区指令、全局配置和其他 MCP，使用非敏感独立目录。workspace-write 不代表可读文件必然只限工作区。[R4,G18]

Cloud Assist 的实际权限和功能取决于当前账号、资源权限及产品 rollout。官方 MCP 接口为私有预览，不能据此声称每个账号都已有可调用子代理、OAuth Agent 或自动设置工具。写入逐项人工确认；秘密页面先关闭 Page context sharing。[G1-G4]

## 新 Router 现场验收必须覆盖

1. 精确版本和新目录，已有 OAuth 路线无不必要重授权
2. bootstrap 与 Control Doc 分离，双向 raw probe 和真实 admission
3. 同一持久 ledger、精确签名链读回、单 runtime、pin/config/deployment 绑定
4. 真实 IDLE、WORKER_POLLING、CONSUMED 和 ROUTER_READY
5. mandatory claim-begin/upload-commit cells，完整输入全部进入同一 context
6. 同线程两轮文字、一次获准 Mac 工具循环、真实结果观察及 ACK
7. 正式 CAS close、facade 进程停止、bootstrap 当前正文和 native 停止分项记录
8. 写成功响应丢失、提前推进读回、并发 START/STOP、dead facade、worker 中断和未知上传的受控故障验收

更多步骤见同包 [升级与现场验收](../ROUTER_UPGRADE.zh-CN.md)。不要从四个成功请求推出无人值守生产可用性、实际并行提速或外部工具副作用恰好一次。
