# 通知、交接与恢复

## 三种证据，不能混用

1. written_outbox：事件写入outbox，包含部署/run/owner/worker/assignment epoch、任务哈希和收据ID；不含模型输入或租约。仅代表本地持久记录
2. read_observer：一个实际运行的观察者读到并记录；仍不等于父级收到
3. acknowledged_parent：父级通过实际可用的渠道明确确认，操作方记录关联证据。Python没有发送通知，只保存该确认

默认notification=outbox_only。初始化时选择parent_tool_attested也只是声明：父级必须检查当前实际可用的任务工具及其schema，使用被授权的目标调用，并核实返回结果。不能因另一个dot有某工具就假设新dot也有；不能填一个不存在的原生推理endpoint。

```sh
python3 portable.py --root "$BRIDGE_ROOT" observe-receipt --owner "$OWNER" --receipt "$RECEIPT_ID" --observer parent-observer
python3 portable.py --root "$BRIDGE_ROOT" ack --owner "$OWNER" --receipt "$RECEIPT_ID" --sha256 "$RECEIPT_BODY_SHA256" --evidence "$VERIFIED_PARENT_MESSAGE_ID" --via manual_parent
```

这些命令由收到证据的实际操作方执行，不得为了让状态看起来成功而伪造ack。ack与receipt的版本、部署、run、owner、ID及内容哈希必须一致。读取、重复回执和重复确认都有一致性校验。

## 谁来触发恢复

必须有明确驱动者：正在运行的父级/观察者、经验证配置的平台事件，或稍后人工检查。独立GUI/Python进程异常、写outbox或心跳消失不保证唤醒dot。观察者和服务同时失败时，报告可能延迟到下一次实际检查。

平台的已分配云任务可能报告turn完成、失败、中断或环境断开，但这不是所有原始GUI子进程的通用通知。环境断开不证明turn结束；应读取任务记录核实，且记录可能短暂滞后。没有本包提供的全天候调度、确切送达时间或exactly-once任务启动保证。

参考平台说明：[事件监测](https://learn.chatgpt.com/docs/dots/tasks-and-memory#event-monitoring)、[已分配工作](https://learn.chatgpt.com/docs/dots/tasks-and-memory#assigned-work)。具体工具能力以运行时实际清单和返回结果为准。

## 保守恢复步骤

```sh
python3 portable.py --root "$BRIDGE_ROOT" recovery --owner "$OWNER"
```

返回仅是action计划，executed=false、notification_delivered=false，绝不自行创建agent或进程。

- 领取后未read：角色租约和任务租约都到期后，可重新assign新的worker；持久epoch和预算阻止旧worker继续提交。最多3次角色启动，队列最多3次领取
- read之后结果不明：持久dispatch=started阻止重新调度，即使任务队列过期或取消也不代表原生推理已经停止
- 父级必须通过支持的平台状态/任务句柄确认“未启动”或“已经停止”，再记录一次明确resolution：
```sh
python3 portable.py --root "$BRIDGE_ROOT" resolve --owner "$OWNER" --job "$JOB_ID" --outcome confirmed_stopped --evidence "$VERIFIED_STOP_EVENT_ID"
```
`--evidence`是操作方确认的关联标识，Python不能替你验证平台事实。如果调用是否启动本身不确定又没有可读的任务句柄，就保持不确定，不能花一次重试预算去启动可能重复的模型任务。

- 已completed但HTTP delivery=waiting：查询现有状态，不重新推理或重发动作。生产者断开会保留ambiguous/cancelled等终态；不自动重放
- 用户停止、认证要求、权限拒绝和配置错误会先持久阻止恢复，即使outbox容量满或写入失败也不放行。需要处理具体原因和必要的新授权；不能换环境绕过拒绝
- 桌面进程只允许运行一次。恢复使用新的部署目录和新随机身份，不复用旧Codex配置/请求。旧目录只保留审计；不能把旧消息自动重放到新实例
- 预算耗尽或部署期限到达即停止，不能用删除状态文件来重置预算

“恢复成功”必须有新worker分配、当前探针/服务身份、真实新随机就绪挑战和实际请求/结果证据。仅写计划、创建任务或出现heartbeat不够。

## 锁与崩溃边界

控制文件和原队列分别使用flock；写入为私有文件、fsync和原子替换。部署身份、角色epoch、队列lease epoch、请求哈希分别核实，不能互相替代。

跨多个文件不是一个数据库事务。关键安全状态先写：开始推理标记先于返回请求；停止标记先于回执；结果已入队但回执失败时，相同ticket与结果可幂等补记。失败可能留下需要人工检查的证据空隙，但不能据此声称已通知、已交付或可以安全重新推理。

同OS用户及其私有共享目录是信任边界。这不是抵御恶意同用户篡改的签名或认证系统；不支持把队列直接暴露到公网或不可信共享盘。
