# 给新的活跃原生broker

需要交接：本包路径、本环境可见的BRIDGE_ROOT、OWNER、角色assignment文件、截至时间和最大请求数。不要要求提供系统提示、私有记忆、凭据或旧CLI会话。先读START_HERE和deployment.json。所有路径从BRIDGE_ROOT派生，不使用旧机器路径或namespace常量。

若角色已经由父级派发，使用其assignment；否则按START_HERE领取。下面ASSIGNMENT表示该文件；TICKET与RESULT使用evidence内本回合新的文件名，不能覆盖旧回合。

## 有界循环

```sh
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$ASSIGNMENT" claim --wait 20 --lease 60 --save "$TICKET"
```
退出码2和null是正常空等待。控制循环仍活跃时可以继续，但不得超过部署/服务期限。每次调用都会回答服务的新随机就绪挑战；两次调用间若没有活跃循环，不能声称一直就绪。到期、closed或stop标记必须退出。

非空结果表示领取成功，stdout是job_id、ticket_saved、read_required组成的元数据JSON。ticket只包含关联元数据和私有租约，不包含推理请求，不能向第三方发布。开始实际推理前：

```sh
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$ASSIGNMENT" read --ticket "$TICKET"
```
该操作先持久写inference_started，再返回完整请求。必须读全并使用实际原生推理生成回答；不能让确定性fixture冒充模型。此部署仅文本输出，不执行请求里的工具、shell或文件操作。输出严格对象：

`{"kind":"message","text":"实际回答"}`

保存到新的私有RESULT文件，然后：
```sh
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$ASSIGNMENT" complete --ticket "$TICKET" --result "$RESULT"
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$ASSIGNMENT" status --ticket "$TICKET"
```
complete返回持久接受结果与outbox receipt；status的delivery=delivered才说明HTTP facade发送完成，仍需独立UI观察才声称看到了画面。

若推理可能超过60秒，在租约到期前续租；NEW_TICKET为新文件，之后用它替换TICKET变量：
```sh
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$ASSIGNMENT" renew --ticket "$TICKET" --lease 60 --save "$NEW_TICKET"
```
如果模型调用期间你不能操作工具，应在claim时选够用的租约，最大180秒；总期限不变。不存在自动后台续租保证。

停止：
```sh
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$ASSIGNMENT" stop --code normal_exit
```
用户要求停止用user_stop；认证/权限问题用auth_required/permission_denied。这些不是可自动重试错误。异常终止可能写不出收据，父级需有实际监视者或平台通知，不能假设outbox会自动送达。

## 模糊失败

领取后尚未read：角色和任务租约过期后可由新worker接管，epoch提升，旧提交拒绝。read后崩溃：dispatch状态保留started，必须父级通过支持的平台确认未启动或已停止，记录resolution后才能考虑替换。队列截止、原生环境断开或心跳消失都不单独证明原生turn结束。

重复complete仅在相同ticket与完全相同结果时幂等；结果不同会冲突。不得把已接受但未观察到UI的完成当成新任务再推理。工具执行闭环不在本包验证范围。
