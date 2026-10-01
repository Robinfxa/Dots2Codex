# 全局 Codex gateway：离线验证阶段

本次发布把全局 gateway 离线原型与统一 Mac 启动器放在同一源码包中；启动器的 wrapper、预检与停止改进保留。
全局菜单仍不支持，生产配置门槛仍关闭，不代表一键全局接管已完成。未修改旧会话、六处源代码绑定或 runner。

## 当前可验证的部分

- 固定、仅 127.0.0.1 的监听端口；私有单实例锁；端口被其他进程占用时失败，不偷偷换端口
- URL 带不可变 generation；generation + session-id + thread-id 分流
- 请求模型和 effort 先验证，再创建线程绑定；同一线程不能中途换模型
- 每个线程独立原生准入、pin、journal、Docs CAS 控制与 facade。绝不复用第一个线程
- 本地 durable claim / spawn-intent / dispatch-intent；未知副作用不重试。只有确认完整的纯文本结果可重放
- 原 facade 继续验证完整历史、tool call/output、交付回执。HTTP flush 不是用户实际交付证明
- 队列、子进程槽位、请求数、连接数、空闲期、总期限都有上限

运行完全离线的两线程演示：

```sh
python3 -m remote_transport.global_fixture
python3 -m unittest discover -s remote_tests -p 'test_global_*.py' -v
```

演示使用本地 fake controller 和真实 CASWorker/RemoteResponsesFacade 代码，但没有调用原生模型、Google、Mac 工具或用户全局配置。输出 production_ready=false 是预期结果。

## 不能据此宣称完成的部分

签名 Google 控制队列、活动原生控制线程 helper、Mac 同步器和每线程 v3 JOIN 接线已有离线端到端测试。真实 Google/native admission、Mac 桌面与 CLI 工具循环仍未验收。Python 不能创建平台原生子线程，JOIN 文件或心跳也不能让已结束的原生线程自动醒来。

本版本 gateway 的 production_ready 和 ready_for_config 恒为 false；global_config.apply 必须看到绑定端口的已验证生产 gateway 才能改配置，因此本原型不能打开配置门槛。离线 fixture 也不满足它。不要手改 ready_for_config 绕过它。

## 首次请求、重启和停止

Mac supervisor 为新线程最多等待 180 秒，让已活动的原生控制线程创建专属子线程。用户提示只保留在有上限的当前 HTTP handler 内存，不写入 admission Doc；验证就绪后才持久保留一次 dispatch intent 并传给该线程。等待期间发送真正的 response.created / response.in_progress JSON 事件，不能只靠 SSE 注释刷新 Codex 的解析事件超时。

同一 gateway response ID 覆盖等待和实际响应；只统一 Responses 外层 ID。原后端 response ID 私下保留，所有 tool item/call ID 与完整历史保持不变。断连发生在 dispatch 前不会稍后自动执行这次提示；超时或取消后需要用户再次提交。dispatch 之后结果未知则永不自动重放。低级 gateway CLI 默认仍是 fail-fast 的 409 admission_pending；Mac supervisor 启用有界等待，真实 Codex 兼容性仍需验收。

现有 ready route 的传输重启复用同一 pin、原生 task 和原 journal，并核对 facade 文件锁、完整历史与控制状态后改绑本地端口。不会重建原生子线程。Stop 先保存本地停止意图，关闭后不会被旧的队列心跳重新启用；再尝试关闭远端队列与每个已建会话的权威控制，包括本地 facade 已关闭的情况。停止本地进程不代表原生子线程已停止。

原生 Google 队列只覆盖一个 generation。新 generation 需要新的有界 JOIN；旧控制线程不会被当作新 generation 的授权。模型/effort 在同 generation 的新线程上可按已审核目录分别选择，但同线程固定。

## 配置安全

只针对显式提供的 CODEX_HOME/config.toml。默认不扫描 profile、项目、登录或凭据目录。不会读写 auth.json、Keychain、系统代理、审批策略或 sandbox。

可选格式保持依赖为 requirements-global.txt 中的 tomlkit。当前执行环境未安装；本次没有安装依赖。相应真实 TOML roundtrip/merge 测试明确跳过（包括用户在受管值旁新增注释的保留行为尚待验收），不能称配置编辑已完整验证。备份、CAS 文件替换、并发编辑保护、崩溃对账和精确原字节恢复的标准库测试可独立运行。

preview 只显示改动和范围；apply 需要精确版本、预览 hash、显式确认和生产就绪探针。每次写入保存私有原字节/hash、postimage 和先行日志，写前重新核对文件身份/hash，然后 fsync + atomic replace + 重读。文件锁只对合作进程有效；无法对任意外部编辑器承诺真正的全局 CAS。

恢复时：精确 postimage 则恢复原字节；有无关编辑则只反向修改仍匹配的受管值；受管值被 CC Switch 或用户修改则冲突停止。原本不存在和空文件分别处理。崩溃/未知结果先 reconcile，绝不盲目覆盖。恢复保留 generation catalog，不停止已运行的客户端。

## 范围提示

官方 CLI 和桌面可共享选定 CODEX_HOME/config.toml，但需分别核对版本和重启。既有/恢复的线程可能保留旧 provider。CLI overrides、profile、允许的项目层和管理策略会影响覆盖范围。监听端口健康、模型出现在菜单或模型自报名称，都不是底层模型/真实推理已经经过该路线的证据。

## 本机安全边界

Origin、Authorization、Cookie、浏览器 Fetch 标头、压缩请求、不准确 Host、混用线程标头均拒绝。控制面另有只存私有文件的随机密钥和挑战应答；转发仅白名单标头，拒绝外网/重定向/递归 endpoint。不会传递官方登录 token。

这里不防同一 Unix 用户下的恶意进程，也不把客户端提供的 thread-id 当密码。同用户进程可读私有状态、伪造线程标识或消耗本地额度；本地 inference 接口没有独立用户身份认证。generation 是隔离标识，不是秘密。如果需要抵御同用户恶意代码，必须另外建立受支持的客户端认证/OS 隔离，不能声称 localhost 自动安全。
