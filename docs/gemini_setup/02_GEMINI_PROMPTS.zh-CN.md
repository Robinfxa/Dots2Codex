# 可复制给 Gemini Cloud Assist 的分阶段提示词

这些文字给 Cloud Console 的配置助手使用。不是 Router 的 join 消息，不应包含 join_code、pin、token 或用户任务正文。每次只复制当前阶段；Gemini 的回答需要真实页面或工具结果确认。[G1–G4]

## P00 总入口 边界与分工

```text
我正在配置 Dots2Codex 修复合并版 Router，在 Mac 上运行官方 Codex CLI，
用 Google Drive 保存协议消息，用 Google Docs requiredRevisionId 做条件更新，
远端 Dots 使用它已经连接的 Google 插件。你只负责 Google Cloud 配置辅助。

请先说明你现在能读取哪些资源、能否执行写入、是否有实际可调用的专业子代理。
不要虚构 OAuth 设置工具，也不要把云侧 IAM 当作个人 Drive 文件授权。
每次只做一个步骤，输出：当前事实、建议操作、影响范围、所需确认、验证方法。
任何 API 启用、IAM、结算、OAuth 设置、共享或删除，先等待我明确确认。
禁止索取或回显 client secret、OAuth token、refresh token、完整 OAuth URL、
Router join_code 或凭据 JSON。敏感页面请先提醒我关闭 page context sharing。

该路径不部署 Cloud Run/VM/Vertex AI，不需要 Gemini API Key。
Mac Python 的授权只在我自己的 Mac 浏览器完成，不能放进 Cloud Shell。
现在只问我目标 project ID 与实际授权账号类型，不执行写入。
```

## P01 只读盘点 不重建已有配置

```text
目标 project ID：<由我填写>。
准备给 Drive 授权的账号类型：<个人 Gmail / 同组织 Workspace / 组织外 Workspace>。
请只读检查当前项目选择、Drive API、Docs API，以及你实际能看到的 OAuth 配置。
无法读取的项列为未核实，并给我 Console 导航路径。
不要因为缺乏可见性就重新创建项目、OAuth client 或文件夹。
请区分项目 ID、项目编号、显示名称和 Drive folder ID。
```

## P02 启用 API

```text
只准备启用 drive.googleapis.com 与 docs.googleapis.com。
先返回目标项目和已启用状态；需要变更的项列成计划，等待我批准。
有真实可执行工具才执行，否则给 Console 点击步骤或一条显式 --project 的命令。
不要附带创建计算资源、组织级启用、全域委派或全盘共享。
结束以服务实际 ENABLED 状态验收，而不是以你生成了命令验收。
```

## P03 受众与测试用户

```text
帮我配置 Google Auth Platform：应用名称 Dots2Codex Remote。
先确认真正登录 Drive 的账号是否属于项目所在组织，再建议 Internal 或 External。
个人 Gmail / 组织外账号使用 External；测试阶段将准确账号加入 Test users。
请说明 Testing 对这两个 Drive scope 的七天授权/refresh token 限制。
不要自动发布应用，不要宣称切换 Production 就一定免验证或永久授权。
邮箱由我在页面填写，不要求我提供密码或验证码。
```

## P04 权限审批

```text
本版 Router 代码要求 drive.file 和 drive.readonly。
请解释：drive.readonly 是账号范围的广泛查看/下载权限，不仅是专用文件夹。
说明当前双 OAuth app 场景的原因、我拒绝广泛只读后必须停在哪里，
以及未来逐文件选择授权方案需要额外开发，不能靠改一个 scope 立即兼容。
不要加入完整 drive、Gmail 或全域委派；不要调整用户的文件 ACL。
先等我批准范围，再指导在 Data Access 添加并保存。
提醒我：Console 加 scope 不会升级已有 token，需要本人重新 consent。
```

## P05 Desktop Client 和 Mac 授权

```text
指导我创建 Desktop app OAuth client，名称 Dots2Codex Mac。
只告诉我下载和私有保存步骤，不索取 JSON 内容。
client-secret.json 和 authorized-user-bidir.json 是不同文件。
真正 OAuth 在 Mac 运行，浏览器回调回到同一台 Mac 的 loopback。
已有双向凭据就先验证，不重新登录、不覆盖旧文件。
远端 Dots 走已有插件，不复制 Mac token，不需要新建 Device/TV client。
```

## P06 403 access_denied 排查

```text
报错：<只放短错误码和脱敏说明>。
先检查准确授权账号、External/Internal、Testing test users、Workspace 管理限制。
不要建议绕过组织策略或非本人的应用验证警告。
我不提供完整授权 URL、state、authorization code 或任何 token。
请给一个最小只读检查，得到结果后再决定下一步。
```

## P07 跨应用 404 排查

```text
Mac 可以读取自己创建的对象，但读连接器创建的对象返回 drive_http_404。
请区分错误 fileId、被删除、ACL 不可见、OAuth scope/应用授权，以及 shared drive 参数。
404 不能单独证明文件不存在。请先核验同一真实 fileId 的双端可见性。
当前有结果已提交，禁止重发推理或改 request ID，也不要先扩大写权限。
若需 drive.readonly，先说明广泛权限并等本人 consent，随后只读验证原对象。
```

## P08 Mac TLS   TTY

```text
这是 Mac 本地问题，不是 Cloud Shell 问题。
对 CERTIFICATE_VERIFY_FAILED，请先检查当前 Python/venv、certifi、SSL_CERT_FILE。
对 stdin is not a terminal，请检查是否用 heredoc 或管道启动交互式 Codex。
不要关闭 TLS，不运行 sudo pip，不全局关闭 Gatekeeper。
只生成检查命令，不索取凭据内容。
```

## P09 配额与费用

```text
请核查今天官方 Docs/Drive API 配额与当前项目的适用值，标注日期和官方来源。
区分不同项目创建时期的 Drive quota 模型、请求数与 quota units。
不要把历史默认值或预览免费当永久承诺。
遇到 429 先提出有界退避；不能将有副作用的未知 CAS 当普通 GET 自动重试。
不要自动购买订阅、提高费用或创建计算实例。
```

## P10 Router 配对或 close 异常

```text
这里是应用协议排错，不要修改我的 Bootstrap/Control Doc。
我将提供脱敏 stage、error code、closed、process_stopped、bootstrap_cleanup、
worker_stop_confirmed 与 facade_alive；没有值就写未取得。
不要要求 join_code、private-pairing.json、join-message.txt 或 controller journal 全文。
正式 Control Doc 关闭未知时应为 RECOVERY_REQUIRED，不得报告 CLOSED。
STOP_ROUTER 必须直接提交或对账 authoritative close，即使 facade 已死。
本地进程停止、Bootstrap 清理和平台 worker 停止分别核验。
若处于 DISPATCH_INTENT 或写入不明，不得重置、改 ID 或重新推理。
配对使用私有 join-code 文件与持久 ledger；未知结果按精确签名事件链对账。
仅列需要 Dots/维护者检查的原始状态和下一步最小证据。
```

## P11 生产化前检查

```text
只做评审，不改配置。
核查受限 scope 的验证/安全评估适用条件、Testing 七天授权、Drive 数据保留、
Bootstrap 当前正文清理与历史版本的区别、native worker 生命周期和调度限制。
默认四小时及128请求，硬上限八小时及128请求。不要把 TTL 当稳定运行证明，也不要宣称外部工具恰好一次。
给出已验证/未验证/需用户决策三个栏目；成本使用官方计费页和项目实际值。
```

## P12 最终脱敏交接报告

```text
请输出配置验收报告，不含任何秘密或个人数据。
包括：项目核验情况、两项 API 状态、Audience 类型、测试用户已核验与否、
Desktop client 类型、两项 scopes 是否已声明及实际 consent 是否确认、
Mac 凭据权限是否 0600、双向 raw probe 是否通过、Router 配对是否真实通过、
并行 cells 和同一 native context 完整输入是否真实验收、四项关闭证据。
未知就填未验证；不要伪造截图、回执、task ID 或命令输出。
不要直接编辑 Router 运行文档来“标记成功”。
```
