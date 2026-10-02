# Lightweight v3：明确选择的新试用入口

本候选实现用一个 Mac Inbox + 每条路由一个原生 Outbox，传输真实 Google Drive 请求/结果文件；默认不写独立心跳，不跑合成 preflight。它仍需实际 Mac、Google connector、原生子任务和客户端的现场验收。监听成功不等于链路或工具兼容性已验证。

## 1. 保留旧安装，先停止并恢复

保留旧程序、`router.json`、授权文件、旧 Docs 和私有事务备份。旧 `START.command` 的默认行为不变；v3 不会改写旧根或迁移尚未确定的请求。

打开 `LIGHTWEIGHT.command`，可先选 Legacy Global status / Stop legacy Global / Restore legacy Global config。旧单会话 Router 仍使用原入口的停止操作。v3 启动会检查旧进程和 config 事务；未停止、未恢复或状态不明确时拒绝新激活。Mac 进程停止不证明 dot 原生子任务已停止，还需要在原 dot 对话中停止它们。

## 2. 第一次启动

1. 打开 `LIGHTWEIGHT.command`，选择 Start lightweight v3 trial
2. 如果缺少依赖，单独确认创建私有 Python 环境并从官方 PyPI 安装已固定版本。没有全局 Python 安装或自动 OAuth
3. 读取已有 Google folder、credential、CODEX_HOME 和精确 model/effort。不会要求选择项目目录，也不会更改旧 `router.json`。多个项目以客户端实际 session/thread 区分；默认授权保留原选择的一组 model/effort。已退役的 `max` 不会被悄悄转换，请显式选择受支持的新组合
4. 确认新 v3 激活及已有凭据复用。默认最多 4 小时、3 条独立路由/3 个原生 child、每路由 128 请求、每个输入/结果 1 MiB。原有 `drive.file` 与 `drive.readonly` 授权被复用，不扩充 scope
5. 允许复制私有 JOIN 后，粘贴到当前 dot 对话。程序不会打印或记录 JOIN code；剪贴板管理器可能保留内容。JOIN 的准确前缀为 `DOTS2CODEX_GLOBAL_JOIN_V3`
6. 检查独立的 first-use config 试用确认：显示精确目标文件、变更字段、新值以及完整前后字节 SHA-256。旧值可能含私密内容，因此不输出原配置全文。确认后才写入新 provider `dots2codex_lightweight_v3`，同时保留可校验备份
7. 完全退出并重开实际客户端，使用全新线程。让原生父按 [Native JOIN guide](LIGHTWEIGHT_NATIVE_CONTROLLER.md) 处理 JOIN。第一个真实任务完成后，状态才可报告该路径的真实往返已经验证

取消 JOIN 复制或 config 确认不会把运行中的激活伪装成停止。可从菜单继续 Copy private JOIN / Apply first-use config trial，或者选择 Stop local v3。

原生准入回执只记录实际提交的 model/effort、`fork_turns=none` 和平台返回 task identity；不独立证明底层模型身份。客户端目录、工具、沙箱与审批仍由原客户端管理。

## 3. 状态、停止与恢复

- Status 分开显示本地 listener、已初始化 JOIN 资源、真实往返结果；不会把 Google 可读、原生 admitted 或本地端口可用说成全面可用
- Resume same activation 只重启已退出的同一私有进程，沿用原端口、expiry、grant、JOIN 与未决栅栏；不重新生子任务，也不重跑推理。端口被占用或本地证据不完整时停止并提示
- Stop local v3 先在本机持久阻止新请求，再异步尝试发布 Inbox stop。Google 不可用会保留“远端停止需对账”；不是直接丢弃未决请求
- 用 Copy native stop request 将停用请求粘贴到同一 dot 对话，让实际父使用原生 interrupt 停止实际 child。仅复制文本不是发送，Inbox stop 也不是平台中断回执。已经 BEGIN 的请求可能仍完成
- Restore v3 config 不需要 Google。只恢复本次拥有的字段；无其他更改时按原始字节恢复，包括原文件不存在的情况。有无关更改时三方合并；同一字段值、注释或备份身份有冲突时停止，绝不覆盖整份文件
- 停止并恢复后才能创建新的 v3 激活。不能把未决 v3 请求自动切回 v2。重新使用旧版本也需要旧版本的新激活/新 JOIN

## 终端入口

```sh
./LIGHTWEIGHT.command start
./LIGHTWEIGHT.command status
./LIGHTWEIGHT.command resume
./LIGHTWEIGHT.command copy-join
./LIGHTWEIGHT.command apply-config
./LIGHTWEIGHT.command stop
./LIGHTWEIGHT.command copy-stop
./LIGHTWEIGHT.command restore
```

需要指定不同安装路径时，用 `--router-config`、`--router-active`、`--legacy-state`、`--state` 和 `--codex-home`；明确重新选择模型时同时传 `--model` 与 `--effort`。保存项目目录不会限制 Global 路由。只影响读取所选 CODEX_HOME 配置的新本地客户端线程；profile、项目、命令行及管理策略可能覆盖它。

当前协议要求完整历史，明确拒绝非空 `previous_response_id`；不能声称已支持上一响应 ID 自动续接。SSE 等待信息不是真实模型 token 流。工具结果必须携带匹配 call ID；不确定的工具交付不会自动重发。
