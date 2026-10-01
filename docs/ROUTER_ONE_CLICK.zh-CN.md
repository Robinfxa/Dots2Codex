# Mac 单消息 Router：安装、配对与结束

本候选把 Router 合并到已发布 `689efa055e6bb2d290f8870e25ff1ebb3a3109bd`，保留
最新长会话、工具续轮、parallel connector cells、文件分块与无正文 timing。不要把旧
OneClick ZIP 直接盖到新版目录，也不要更新正在运行的 worker。

## 先确认边界

- “一键”指安装后 Mac 启动，加一次私有 join 消息；Dots 端仍需已授权的 Google connector、
  平台真实 native admission，以及保持活跃的原生 worker。Python 不接纳、唤醒或调用模型
- 默认 4 小时、128 次模型请求；硬上限 8 小时、128 次。工具结果续轮也消耗模型请求预算。
  不承诺无限运行、平台额度绕过或 8 小时原生任务存活
- bootstrap Doc 与正式 Control Doc 必须不同。只有正式 Docs CAS 控制执行；pin、原生身份、
  requiredRevisionId、one-use begin/input、已有工具重放保护保持有效
- 需要 Mac 明确选定的现有 authorized-user 文件，已有 `drive.file` 与 `drive.readonly` 授权。
  安装器不会进行登录、扩展授权、索取另一端的 OAuth 或复制凭据
- Google Doc 历史版本可能保留 pin/config。清空当前正文不是历史删除，HMAC 也不是加密。
  transport folder/Docs 只应开放给本来获授权的参与方
- 完整新 Router 的真实 Mac/Google/native 配对和多小时耐久性仍未现场验证，参见
  [验收状态](ROUTER_VALIDATION.md)

## 首次安装

在 Mac 独立解压完整候选目录，先审阅源码、manifest 和下面的操作。确保 Python 3.11+
与官方 `codex-cli 0.159.2` 已安装。执行 `INSTALL_ROUTER.command`：

1. 创建目录内独立 `.venv-router`，从已声明的 PyPI 依赖安装；不修改全局 Python
2. 询问已批准的 transport folder ID、Mac 自己的 authorized-user 文件绝对路径、Codex 工作目录
3. 只读检查授权文件记录的 scopes 和 folder 可见性，写 0600 `~/.config/dots2codex/router.json`

包内没有 OAuth 文件；凭据保留在用户选择的原路径。配置仅记录路径，不复制 token。
macOS 如果提示安全验证，应使用正常系统验证与打开方式；不要绕过系统安全警告。

Dots 端使用同一候选源码和更新后的 [join 指南](ROUTER_JOIN_V1.zh-CN.md)。Bootstrap
内部协议已升级 V2，旧 V1 helper 不兼容；用户复制消息的 `DOTS2CODEX_ROUTER_JOIN_V1`
标记保留只为入口格式，不表示旧 helper 可以混用。

## 日常启动

1. 运行 `START_ROUTER.command`
2. Mac 创建两份独立 Doc、一个小型 forward probe，显示并复制一条私有 join 消息
3. 把整条消息发送给获授权的 Dots。不要放公开 issue、日志、截图或 shell 参数；发送后清空剪贴板
4. Dots 从平台取得真实任务身份，用私有文件读取 join-code，验证 signed root 与双向 raw-file probe；
   然后领取匹配 pin/config、解析正式 Control Doc 的 IDLE，并回写签名 polling ack
5. Mac 验证完整证据后，清空 bootstrap 当前 bundle 正文，保存可核验的操作记录，再启动
   loopback facade。出现 `ROUTER_READY http://127.0.0.1:…/v1` 后在终端进入 Codex

Mac 保留 on-request approval、workspace-write sandbox 和既有 Codex provider/retry 设置。
默认端口 0，由系统分配。版本不匹配会停止，需要单独真实验收。

原生 worker 必须使用生成的 `connector_cell claim-begin` 与 `upload-commit` 路径；Router
runtime 标记要求 all-verified upload batch，不能静默退回旧串行提交路径。

## 状态、停止与失败

- `ROUTER_STATUS.command`：只读查看本地阶段、已确认关闭状态、facade 是否存活且 PID 身份匹配、
  bootstrap 阶段与 bridge 状态；不打印 join-code、bundle、凭据或任意日志内容
- `STOP_ROUTER.command`：先写持久 stop 标记，等待正在启动的临界区退出，再直接访问正式
  Control Doc 提交或对账 CAS close。即使 facade 已死或 ready 文件不存在也不跳过 authoritative close
- `closed=true` 只代表正式控制记录已核验关闭；还要看 `process_stopped=true`。bootstrap 清理状态
  单独报告。不会把无法读取 control 或未知写入报告为成功关闭
- 关闭会阻止未来 admission/claim/begin；已经消费的执行可能仍完成。`worker_stop_confirmed=false`
  表示没有平台停止回执，Dots 仍需读取 close 并合作停止/对账已知结果
- `RECOVERY_REQUIRED`：保留私有 runtime、操作 ID、响应与日志。未知 create/upload/CAS 不盲重试，
  不删除 journal 重来。只有明确 CAS 拒绝可在下一次显式 stop 重新规划关闭；其他未知 close 只读对账
- facade 子进程须等待持久 PID grant 才加载 facade；启动超时/取消会回收已知子进程。
  仅有同名或重用 PID 不授权发送信号。OS/磁盘/网络硬故障仍可能需要人工核对

证据文件、Google 对象和 Docs 不自动删除，不自动改权限。不承诺 exactly-once 外部工具副作用。
完整升级和现场检查顺序见 [升级与验收](ROUTER_UPGRADE.zh-CN.md)。
