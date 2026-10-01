# 安装与现场验收记录

日期 ________  操作人 ________  实际包 SHA256 ________

逐项填写 PASS、FAIL 或未验证，不用“应该成功”替代证据。秘密不填入公开记录。已有 OAuth 的操作者跳过不必要的新建与授权，只复核仍适用的条件。

## 配置与版本

- [ ] 最终包外部校验文件通过，解压到新目录，没有旧 v1 代码覆盖
- [ ] 修复版源码、指南及 Dots helper 是同一候选
- [ ] 旧会话的权威 close 已确认，未知任务已对账，没有活动旧控制方
- [ ] 项目和真实 Drive 账号明确，Drive API 和 Docs API 已启用
- [ ] Audience、Test users、组织限制及 Testing 七天条件已核对
- [ ] 理解 drive.readonly 广泛范围，已获本人批准；未扩大读写或分享
- [ ] 已有凭据优先复用，私有文件 0600、父目录 0700，不覆盖旧授权
- [ ] Gemini 仅作辅助，MCP 或子代理能力实际可用性已核对
- [ ] secret、token、完整 OAuth URL 和 join_code 未发送给 Gemini
- [ ] Python 3.11+、TLS、依赖与精确 Codex 0.159.2 通过
- [ ] 配置只保存凭据路径，已有配置迁移经过审核

## 配对与原生执行

- [ ] Dots Google 工具、raw-file 下载和真实 native admission 槽位具备
- [ ] 同一持久私有 ledger 可写，未复制、回滚或更换目录绕过拒绝
- [ ] 新 bootstrap 和 Control 是不同 Doc，单 tab 及 revision 读取完整
- [ ] join_code 私有文件为 0600，经 --join-code-file 使用，不进参数或环境变量
- [ ] Mac 到 connector 精确 raw probe 的 ID、title、parent、MIME、hash 和 nonce 通过
- [ ] connector 到 Mac 精确 raw probe 通过，未知上传没有重传
- [ ] admission 身份来自平台实际接纳，精确签名事件经 fresh readback 验证
- [ ] pin/config/deployment/hash/session/lifetime 和五字段 canonical config 绑定通过
- [ ] 仅一个新私有 runtime，reservation、receipt、模式和 source hashes 通过
- [ ] strict Control IDLE 通过，WORKER_POLLING 精确 ack 已验证
- [ ] CONSUMED 当前 bundle 已清空，仅声明当前正文，不声明历史已删
- [ ] ROUTER_READY 后 Codex 在真实 TTY 打开

## 请求与结果

- [ ] claim-begin cell 自行 fresh read、reservation、claim 和 begin，没有提前 tick
- [ ] 完整 cell await 到结束，未遗弃写入 promise
- [ ] input 仅消费一次，所有连续 chunk 到 EOF 进入同一真实 native context
- [ ] 两个同线程文本回合成功，previous receipt 与历史验证通过
- [ ] 本地工具在已批准 workspace 执行，实际文件字节独立检查
- [ ] upload-commit 所有对象 metadata 和 raw bytes 通过后才 result CAS
- [ ] 无旧串行提交、重复上传、重复 begin 或重复推理
- [ ] Mac 实际观察结果后 ACK，全部应确认请求已 confirmed

## 关闭与故障

- [ ] 退出 Codex 后执行 STOP_ROUTER，停止过程与启动临界区协调
- [ ] 正式 Control Doc 权威 closed=true，不只看本地进程或状态文字
- [ ] process_stopped 独立核验，PID 与 OS 身份一致
- [ ] bootstrap_cleanup 成功、失败或未知已单列
- [ ] worker_stop_confirmed 有真实证据；没有就写未确认
- [ ] facade 已死或 ready 缺失时仍走权威 close
- [ ] 未知 close/上传/CAS 保留原操作只读对账，不删 journal 重来
- [ ] RECOVERY_REQUIRED 未误报 CLOSED，已消费执行是否完成单独记录
- [ ] 私有 runtime、ledger、receipt 与对象保留，未发布秘密

## 验收结论

```text
release_integrity: PASS / FAIL / 未验证
existing_oauth_reused_or_new_approved: PASS / FAIL / 未验证
bidirectional_raw_probes: PASS / FAIL / 未验证
one_message_bootstrap: PASS / FAIL / 未验证
native_admission_and_full_input: PASS / FAIL / 未验证
parallel_cells_live: PASS / FAIL / 未验证
codex_two_turns_and_tool_loop: PASS / FAIL / 未验证
final_ack: PASS / FAIL / 未验证
authoritative_close: PASS / FAIL / 未验证
process_stopped: PASS / FAIL / 未验证
bootstrap_cleanup: PASS / FAIL / 未验证
worker_stop_confirmed: PASS / FAIL / 未验证
multi_hour_endurance: PASS / FAIL / 未验证
unresolved_items: ...
```
