# Mac 统一启动器

本候选基于已发布 `00156e4621444eefa90752545dc1a40521639fb1`，新增共用后端。
双击根目录 `START.command`：选择 Global 桌面模式或独立单会话模式；第一次完成受控设置，以后复用健康环境和已保存设置。
`Router.app` 是可选的原生 AppleScript applet，必须在 Mac 构建，最终也打开同一个 START 入口。

Global 模式已接入稳定多线程网关、全局控制器 JOIN 和带证据门槛的配置确认，详见 [桌面全局模式](DESKTOP_GLOBAL.zh-CN.md)。下列历史单会话步骤继续适用于菜单中的 Single-session；全局模式不使用单会话 facade 作为所有线程的共享 worker。

## 第一次双击

1. 检查公开文件 manifest 与 Python 3.11+。找不到 Python 时显示 [Python 官方安装说明](https://www.python.org/downloads/macos/)，可选择已有解释器；不自动安装、sudo、修改 PATH 或 shell rc
2. 询问是否在 `~/.config/dots2codex-launcher/environments/` 创建私有环境并从 PyPI 安装固定直接依赖。只有明确同意才创建或安装；不改全局 Python。间接依赖不是完整 lockfile
3. 只从已存 Router 配置、明确环境变量 `DOTS_GOOGLE_AUTHORIZED_USER_FILE`、默认 `~/.config/dots2codex/authorized-user-bidir.json` 和用户选中文件发现凭据。不会扫描浏览器、Keychain、ADC、gcloud、Codex auth 或整个 HOME
4. 显示凭据路径并确认复用。解释 `drive.readonly` 可读取帐号较广的 Drive 内容，folder 约束只是应用层约束。只接受已有 authorized-user 记录及 `drive.file`、`drive.readonly`，不自动 OAuth 或扩权
5. 填写已有专用 folder ID，或无 query/fragment 的 `https://drive.google.com/drive/folders/ID`。选择已有工作目录、已有官方 `codex-cli 0.159.2`，核验版本与必要 flags；找不到时给出[官方 Codex 说明](https://github.com/openai/codex)，不自动安装最新版
6. 从本版固定能力表选择模型，再选择该模型允许的 effort。两个值一起保存；这不是实时帐号可用性查询或底层模型证明
7. 只读检查 credential 格式、scope 记录与精确 folder 的 ID、未删除状态和 folder MIME。检查通过前不创建工作目录或写配置；本入口要求工作目录已经存在
8. 展示路径、folder、workspace、Codex、model/effort 与保存位置，再确认保存 0600 配置。配置只保存凭据路径，不复制 token
9. 确认开始新会话后，沿原 Router 路径创建独立 Control Doc、bootstrap Doc 和 forward probe。再询问是否复制本轮私有 join 消息；发送给获授权 Dots 后清空剪贴板

Google 只读预检成功只说明精确 folder 可读。实际跨 OAuth app 访问、双向 raw-file probe、真实 native admission、WORKER_POLLING 和 facade ready 仍须通过原协议。
只有这些关卡通过才显示 `ROUTER_READY`，随后在同一个 Terminal 中打开 Codex。

## 日常入口与控制

```sh
./START.command                     # 打开 Global / Single-session 菜单
./START.command menu                # Global 启动/状态/停止/恢复；Single-session；设置/退出
./START.command settings            # 修改新会话默认值，活动会话时拒绝
./START.command status              # 本地阶段与实际 loopback readiness，不触发安装或 OAuth
./START.command stop                # 原 authoritative CAS close 与精确 PID 停机
./START.command --model gpt-6.1-sol --effort xhigh
```

兼容保留 `INSTALL_ROUTER.command`（设置）、`START_ROUTER.command`（启动）、
`ROUTER_STATUS.command` 和 `STOP_ROUTER.command`，以及对应 `mac_router/` 入口。
模型/effort 参数只对新会话有效；CLI 覆盖只影响本次新会话，设置菜单才修改保存的默认值。
已配对 runtime、pin、model catalog 和 admission 不能原地换模型或改 hash。

已有活动、正在配对或 stale/incomplete 状态时不创建新 Docs：只显示状态、待配对消息复制、停止或取消。
已经绑定第一个客户端的 facade 不能供第二个客户端复用；启动器不提供会冒充多会话支持的“打开另一窗口”。
只有已核验关闭且进程停止，或已安全 ABORTED 且进程停止，才允许下一次新配对。

状态会分开报告 `router_ready`、`closed`、`process_stopped` 与 `worker_stop_confirmed`。
`worker_stop_confirmed=false` 不能解释为平台 worker 已停止；已消费的执行仍可能完成。
停止仍使用原控制记录、操作 ID、CAS 对账与 PID 身份校验，不删除凭据、journal 或历史证据。

## 环境安装与失败恢复

- 环境位于最终唯一版本路径，创建后永不 rename，避免 venv shebang 指向旧路径
- 完整检查 Python 版本、venv prefix、直接依赖版本、必要 imports、`pip check` 后，才原子更新 `environment.json` 指针
- 后续启动实际复检健康环境，不因目录存在就认为成功，也不每次重新联网安装
- 安装锁拒绝并发双击；取得锁后再次检查已有环境，避免重复安装
- 安装或网络失败只保留标记为 incomplete 的候选，不覆盖健康指针。重试要再次明确同意；旧目录保留，不自动清理
- 符号链接或不安全的私有文件/目录权限会拒绝，不自动 chmod 或跟随任意路径
- 没有 GUI 时可回退到实际 TTY；没有 TTY 不默认同意。取消、AppleScript -128、超时或 EOF 都不是批准
- 原生对话框内容通过 argv 传递，不插入 AppleScript 源码。无需 Accessibility 或 System Events
- 网络错误不能推断成 token 失效，更不能因此自动申请 OAuth
- setup 失败可以从头重做只读校验；进入 Router 资源创建后的取消走原 cleanup。未知 create/upload/CAS 保留 RECOVERY_REQUIRED，不重放

剪贴板只写当前 join 消息，必须主动确认。失败时显示实际失败和私有文件路径，配对仍可等待；不会假称已复制。
默认 stdout、普通状态与安装记录不打印 join-code、OAuth、token、provider 错误正文或 bundle。
底层 `python -m remote_transport.router_mac start --copy-join` 是显式复制开关；不再默认打印完整 join 正文。

## 构建可选 Router.app

在 Mac 的完整候选目录中运行：

```sh
./mac_router/build-app.command
# 或选择一个尚不存在的输出路径
./mac_router/build-app.command /some/new/path/Router.app
```

脚本通过系统 `osacompile` 构建 applet，并按 manifest 内嵌完整公开源码资源。
不把 venv、OAuth、config、runtime 或 journal 写入 app 包。移动 app 不依赖旁边另一份仓库。
应用通过 LaunchServices 打开内嵌 START.command，保留真实 TTY；不用跨应用键盘/点击模拟。
构建产物未签名、未公证，不绕过 Gatekeeper 或任何系统安全警告。

## 验收边界

当前提供 Linux 下的后端/注入端口、原协议回归、shell 语法与 app 模板检查。
**尚未在 Mac 编译或运行 app，也未现场验收 Finder、原生弹窗、Terminal、Gatekeeper、Intel/Apple Silicon、实际 Python/Codex 发现、Google 或 native 推理。**
官方依赖安装、OAuth、真实 Google 写入与全局配置改动均未作为本次离线测试执行。
测试记录见 [统一启动器验证](UNIFIED_MAC_LAUNCHER_VALIDATION.md)。

升级仍要求独立 checkout 与新配对；保留旧运行目录、pin、journal 和证据。
原 CAS、停止语义、六文件 source binding、parallel connector-cell 与真实 admission 约束不变。
