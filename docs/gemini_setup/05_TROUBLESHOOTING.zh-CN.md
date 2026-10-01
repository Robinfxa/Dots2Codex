# 故障排查与未知结果对账

先确认操作端是 Mac、Cloud Console 还是 Dots，再收集短错误码与脱敏状态。只做一个最小检查，拿到真实证据后再决定下一步。不要上传凭据、完整授权 URL、join 消息、journal 全文或未脱敏日志。

## OAuth 与资源可见性

**403 access_denied 或 org_internal：** 核对真正登录的账号、Audience、Test users 和 Workspace 管理限制。组织外账号不能冒充内部成员；不要绕过组织策略或陌生应用的验证警告。[G6]

**invalid_grant 或过期 refresh token：** Testing 下本指南的 Drive 授权受七天限制。由本人按需重新 consent，写入新私有输出文件，验证后审核配置切换；不删 journal，不复制 connector token。[G6]

**router_requires_drive_file_and_drive_readonly_scopes：** 核验应用实际请求、用户批准范围和本地记录。只修改 JSON 的 scopes 文字不会取得权限。不接受广泛只读就停止，不能删除检查绕过。[G7]

**drive_http_404：** 逐项排查精确 file ID、ACL、应用访问授权、scope、删除或 shared-drive 参数。404 可以表示不存在或不可访问，不足以证明结果丢失。按原 ID 双端只读对照，不重新推理或扩大读写权限。[G8]

**FOLDER_NOT_VERIFIED：** helper 的 check 仅对给定 folder 做 metadata 读取；核实真实目录类型、trashed 与授权。通过 check 也不证明双向 raw-file 下载或 Docs CAS。

**DESKTOP_CLIENT_REQUIRED 或 UNEXPECTED_OAUTH_ENDPOINTS：** 重新核对来自本项目的 Desktop 下载文件。工具拒绝 web/installed 混合配置与非预期 OAuth endpoint；不要手改文件使其通过。

**CREATE_INTENT 已存在或文件夹创建结果不明：** 保留同一 receipt 并只读核对真实云端结果。不要换 receipt 名称或另一个 SDK 重试创建，不按名称猜哪个目录属于本次操作。

## Mac 环境与启动

**CERTIFICATE_VERIFY_FAILED：** 检查当前 venv、certifi、SSL_CERT_FILE 和组织 TLS 代理。使用批准的 CA；禁止关闭 TLS 校验或盲目 sudo pip。[G17]

**stdin is not a terminal：** 在真实 Mac Terminal 直接运行 .command，避免 heredoc、管道和无 TTY 后台进程。不要用关闭审批的无头模式替代。

**codex_version_requires_live_acceptance：** 核验实际二进制路径与精确版本。保留 0.159.2 基线，不改检查伪装通过。其他版本需单独兼容验收。

**已有配置或活动状态被拒绝：** 运行 STATUS，核验旧会话与正式控制记录，按同包升级规程处理。不要直接覆盖 router.json、删 router-active.json 或用 killall python/codex。

**router_bootstrap_wait_timeout：** 检查 Dots 活跃上下文、真实 stage、操作记录和是否有未知写入；保留现场，核验正式 control 与 bootstrap。不要连续 START 创建更多孤儿资源。

## 配对与执行

**agent thread limit reached：** 先按平台规则取得真实执行槽位。Python、文件名或计划中的 task ID 都不是 admission。

**forward 或 reverse probe 失败：** 核对精确 ID、name/title、parent、MIME、raw bytes/hash 与 nonce；不能使用 extracted text 代替原字节。没有双向证据就不宣称就绪。

**ledger 拒绝旧观测或分叉：** 保留同一 ledger、plan 和 raw readback，对账本次精确签名事件链。不要换 ledger、调时钟或回滚目录绕过。

**materialize 中断或 runtime/source hash 不同：** 只验证同一原 runtime 是否完整；未知中断不能新建第二个 runtime。不要删 receipt 或用旧源文件替换来继续。

**Docs stale revision 或写响应丢失：** 区分明确 CAS 拒绝与未知写入。使用 fresh readback 和原 operation ID/精确已签名链证明；对端推进是可对账情况，不是自动成功，也不是重放理由。不能把所有 HTTP 400 都视为未写入。[G12]

**input_exposed 后读取不全：** 固定 hash，从原输入文件按连续 offsets 读到 EOF，并让全部文本进入同一实际接纳的上下文。读不全则停止；不能再调用 input、换 worker 或靠截断文本推理。

**upload-commit 单项未知：** 对账该精确 immutable object；已知完成的并行项不重传。所有对象验证完成之前不能 result CAS，也不改用旧串行循环。

**504 或 remote_wait_budget_expired：** 只说明 HTTP 等候未完成，不证明推理没开始。按原 request ID 找迟到结果，实际观察后才 ACK；不重发 prompt 或换 ID。

**429 或配额不足：** 按实际项目限制有界退避，遵守 poll 返回间隔和 read budget。只读 GET 的重试与未知写入对账分开；不每秒轮询、不自动购买额度。[G10,G11]

## 停止与恢复

始终使用 `STOP_ROUTER.command` 走权威 close。facade 已死、ready 缺失或启动中都不能跳过正式 Control Doc 的关闭或对账。

`RECOVERY_REQUIRED` 表示权威关闭等关键状态尚未建立。不能把本机进程退出当 CLOSED，也不能把 bootstrap 清理当正式 close。未知 close 只读对账精确 operation ID；明确 CAS 拒绝才允许下次显式 stop 重新规划。

分别记录 closed、process_stopped、bootstrap_cleanup 和 worker_stop_confirmed。没有 native 停止回执时，报告未确认并让 Dots 读 close 后合作退出；不宣称所有任务已取消。不要按同名进程或重用的 PID 杀进程。

## 给 Gemini 的脱敏报告

```text
操作端：Mac / Cloud Console / Dots
包：2026-10-01 修复版 Router 加 G2 指南
步骤：安装 / OAuth / probe / bundle / ready / request / close
短错误码：<值>
bootstrap_stage：<值或未取得>
closed：<true / false / 未核实>
process_stopped：<true / false / 未核实>
bootstrap_cleanup：<成功 / 失败 / 未知>
worker_stop_confirmed：<true / false / 未核实>
是否可能已 begin：<是 / 否 / 未知>
已做的只读核查：<概述>
请只建议一个最小检查，不写入任何协议状态。
```

先前 scope 修复后原 result 可读，只支持当时的跨应用授权诊断；不能把所有 404 归因于 scope。代码防重放也不构成外部工具副作用的恰好一次保证。
