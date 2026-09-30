# 从这里开始：两端角色不能混淆

本包用于有界、文本推理桥接。桌面端运行官方Codex和同一网络环境中的loopback facade；broker端运行一个已经活跃、获授权、能实际推理的原生agent。两端只需共享文件，不假定共享localhost。Python只能管理数据，不能自动调用平台原生agent工具。

## 目录和所有权

所有命令从本包目录执行。操作方指定一个此前不存在的私有共享目录，记为BRIDGE_ROOT；两端看到同一目录内容即可，本地绝对路径可以不同。OWNER只是本部署的关联标识，不是认证凭据。不得预建该目录，由init独占创建。

```sh
python3 portable.py --root "$BRIDGE_ROOT" init --owner "$OWNER" --seconds 600 --max-requests 3
```

init创建deployment.json、evidence/和控制目录。此后证据只放evidence/；不得预建runs/内的queue或isolated-cli。桌面bootstrap独占创建它们。此前“broker为了写证据先创建桌面启动目录”导致启动冲突，本协议明确禁止这种做法。

## 首次部署探针（两端分别运行）

桌面端：
```sh
python3 probe.py --root "$BRIDGE_ROOT" observe --role desktop
python3 probe.py --root "$BRIDGE_ROOT" offer
```

broker端在自己的命令环境中：
```sh
python3 probe.py --root "$BRIDGE_ROOT" observe --role broker
python3 probe.py --root "$BRIDGE_ROOT" answer
```

桌面端在120秒内：
```sh
python3 probe.py --root "$BRIDGE_ROOT" verify
```

这只证明当前共享文件nonce往返。观察到的PID/namespace会动态记录，不是配置常量；同一环境测试不会伪称跨环境。探针不证明网络、原生推理槽位、OAuth或通知能力。

## 启动顺序

1. 已活跃的原生broker领取角色：
```sh
python3 portable.py --root "$BRIDGE_ROOT" assign --owner "$OWNER" --role broker --worker broker-one --native-capability-attested --save "$BRIDGE_ROOT/evidence/broker-assignment.json"
```
`--native-capability-attested`是操作方声明，不是Python检测到了模型。没有实际原生能力就不能声明。

2. broker按BROKER_HANDOFF.zh-CN.md开始有限claim；每次最多20秒。桌面尚未创建ready.json时正常等待，可在部署期限内继续。broker不创建queue，不伪造ready.json。

3. 桌面端先查看计划，不启动任何进程：
```sh
python3 desktop_bootstrap.py --root "$BRIDGE_ROOT" --owner "$OWNER"
```
4. 确认当前环境获授权并选择官方可执行文件后，由桌面操作方显式启动：
```sh
python3 desktop_bootstrap.py --root "$BRIDGE_ROOT" --owner "$OWNER" --codex "$CODEX_BIN" --execute
```
这创建新的HOME/CODEX_HOME/workspace并选择唯一桥接provider，保留read-only/on-request。不修改原默认Codex。不要在因明确访问拒绝而受限的环境中换路径规避拒绝。

5. broker的claim/read/infer/complete顺序见交接文档。完成接收不等于桌面已显示，应检查status的delivery并按需要核实UI。

正常结束使用CLI自己的退出方式。桌面bootstrap清理自己的服务/子进程，尽力恢复它保存的TTY属性。强制kill或整台环境崩溃可能来不及finally；不承诺总能写回执或恢复终端。

## 恢复和通知

outbox文件只表示written_outbox。读取者可记录read_observer，只有父级明确确认后才可写acknowledged_parent；写文件本身不会唤醒dot。云任务生命周期通知与原始GUI/Python进程不是同一种能力。见RECOVERY.zh-CN.md。

桌面只允许一次启动，恢复必须新部署目录，不重放旧请求。broker最多3次角色启动且最多3次队列领取；具体上限以deployment.json为准。用户停止、认证或权限错误禁止自动恢复。已开始但结果不明的原生推理必须先确认未启动或已停止，不能仅凭心跳/队列租约过期就重复调度。

## 给另一位父级/调度者的机器格式

角色已分配后，可生成不含请求/租约的数据包：
```sh
python3 handoff.py --root "$BRIDGE_ROOT" --owner "$OWNER" --assignment "$BRIDGE_ROOT/evidence/broker-assignment.json"
```
输出符合schemas/handoff.schema.json。它指定本环境可见路径、assignment相对路径、文档入口、截止时间与预算，不自动发消息或启动agent。另一环境必须先核实自己的路径映射和实际可用工具，不能直接照抄本机绝对路径。
