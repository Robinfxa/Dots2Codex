# Google Cloud 和 Mac 首次设置

适用：2026-10-01 修复合并版 Router；文档版 G2。重点官方资料于 2026-10-01 复核。

## 1  先明确 Gemini 帮你配置 不自动变成 Router 的推理模型

目标体验是：一次完成配置后，在 Mac 双击 `START_ROUTER.command`，把自动生成的一段配对消息发给 Dots，等待 Codex 打开。

Google Cloud 内置的 Gemini Cloud Assist 能提供云资源问答、检查和操作建议。官方 MCP 文档描述了 Root Agent 路由专业子代理，并通过用户授权执行变更；该 MCP 接口目前为私有预览，不能据此假定每个控制台账号都开放全部能力。Cloud Assist 继承当前用户权限，不是“超级管理员”；官方也未保证它能直接修改 Google Auth Platform 的全部 OAuth 页面。[G1–G4]

本指南把 Gemini 用作**配置助手**。真正的请求仍走：

```text
Mac 官方 Codex
  → 本机 127.0.0.1 facade
  → Google Drive 消息文件 + Google Docs 条件更新
  → 已获平台接纳的 Dots/native worker
  → Drive/Docs → Mac Codex
```

Gemini Cloud Assist、Google Cloud 项目、Google Drive 账号、Dots 连接器和 native worker 是不同的身份/能力边界。给 Cloud IAM 加角色不会自动授权读取个人 Drive；Gemini 能读云资源，不等于能查看你的 Mac 文件。

**这条路径不要求部署 Cloud Run、虚拟机、Vertex AI、数据库或公网服务。也不要求 Gemini API Key。** 不要为了配置助手去新增与 Router 无关的云资源。[R1–R4]

## 2  先选路线 已有环境还是从零开始

### 已有环境

已完成 Mac 双向 OAuth 和原长会话测试，可以复用自己的 `authorized-user-bidir.json`、同一个私有 transport 文件夹以及已验证的 Codex 0.159.2。直接做第 7 节的安装；不要重新执行授权脚本，更不要复用历史 pin、Control Doc 或 journal。

### 首次安装

依次做第 3–7 节。每个阶段都等真实返回或页面状态通过，再进入下一步。新建/改权限的操作要先核对项目和账号；失败不自动重复创建同名文件。

| 参数 | 填什么 | 不要混淆 |
|---|---|---|
| Cloud project ID | 顶部项目选择器里的真实 ID | 不是项目显示名称，也不是项目编号 |
| Google 账号 | 真正用于 Drive 授权的账号 | 可以不同于 Cloud 项目管理员；须满足 Audience 和 ACL |
| OAuth client | 本项目的 Desktop app | 不是 API Key、服务账号或连接器 token |
| transport folder ID | 你批准使用的专用 Drive 文件夹 ID | 不是 Cloud 组织/资源文件夹 ID |
| authorized-user 文件 | Mac 自己完成 consent 后的私有 JSON | 不是下载的 client-secret JSON |
| workspace | 独立测试工作目录 | 不要选 HOME、凭据目录或含私密资料的大目录 |

## 3  打开 Cloud Console 内置 Gemini

**【Gemini 可辅助】【必须你确认】**

先登录 Cloud Console，在顶部选择目标项目。点击工具栏 Gemini/Cloud Assist 图标。界面名称随语言或 rollout 变化，以实际页面为准。没有图标时，从“Admin for Gemini / Gemini for Google Cloud”进入项目级设置；不要在整个组织上批量开启。[G1–G3]

官方项目级设置指南目前使用 `Get Gemini Cloud Assist`，并提供必需/推荐 API 的列表。按页面展开列表，区分必需项和可选项；仅批准本项目的必要设置。不要为了聊天去开启调查、数据库、App Management 等与本任务无关的可选服务。使用 Gemini Cloud Assist 所需角色与目标资源权限分别核对。按当前官方 IAM 页面选择适用的最小权限；已有适当权限不重复授权，不为方便升级到 Owner/Editor。[G2,G3]

Cloud Assist 缺失并不阻塞 Router。可以按本指南手动完成后续步骤。不要购买 Code Assist/企业订阅来代替尚未查清的权限问题。

先复制 `PROMPT_GEMINI_START.txt` 给它。要求它每次只做一步，并区分：已核实、建议操作、等待你批准、当前无法核实。

**隐私设置：** 在处理 OAuth 客户端/凭据页面前关闭 Gemini 的 Page context sharing，或离开显示秘密的页面后再提问。不要把 client secret、token、完整 OAuth URL 或 Router join_code 发给 Gemini。聊天历史和项目权限有自己的可见性规则，不能把它当秘密保险箱。[G1]

## 4  创建 选择项目并启用两个 Workspace API

**【Gemini 可辅助】【必须你确认】**

1. 已有项目就选中它；没有时在项目选择器点“新建项目”，名称可用 `Dots2Codex`。组织/父资源由你确认归属；不要根据名字猜它属于你。
2. 顶部搜索 `Google Drive API`，核对当前项目后点“启用”。
3. 再搜索 `Google Docs API`，同样启用。看到“管理/已启用”即停止，不重复操作。
4. 在“API 和服务 → 已启用的 API 和服务”检查这两项。[G5]

可发给 Gemini：

```text
只检查当前所选项目的 Google Drive API 和 Google Docs API。
先列出已启用/未启用/无法读取。需要启用时说明具体 API 和项目 ID，
等待我确认；不要创建计算资源，不要改组织 IAM，不要开 Vertex AI。
```

会使用 Cloud Shell 的管理员可以先只读查询：

```sh
gcloud services list --enabled --project 'YOUR_PROJECT_ID' --format='value(config.name)'
```

确认后，才执行显式变更：

```sh
gcloud services enable drive.googleapis.com docs.googleapis.com --project 'YOUR_PROJECT_ID'
```

这里的 `YOUR_PROJECT_ID` 必须换成真实值。Cloud Shell 只用于 Cloud 配置；下一节的 Mac OAuth 不能照搬到 Cloud Shell。[G15]

**费用：** 不把“启用 API”说成“所有服务永远免费”。Google 当前将标准 Drive API 使用列为不额外收费，并公告额度和未来计费变化；项目还可能有其他资源、存储或订阅费用。Cloud Assist 的预览价格也不能当长期承诺。只在出现实际结算要求时核对服务和组织政策，不为此默认新开付费资源。[G10,G14]

## 5  设置 Google Auth Platform 按实际 Drive 账号选择受众

**【Gemini 可辅助】【必须你确认】**

### 5 1 Branding   品牌信息

进入“Google Auth Platform → Branding”。未初始化时点“开始使用”。应用名填 `Dots2Codex Remote`；支持邮箱、开发者联系邮箱用你能接收邮件的地址。应用显示名称可以变更，不把它当 API 资源 ID。[G5]

### 5 2 Audience   受众

准备使用普通 `@gmail.com` 或组织外账号时选 **External**。只有所有授权使用者都属于该组织时才考虑 **Internal**。项目挂在组织下面，不等于个人 Gmail 自动成为该组织用户。[G6]

个人测试先保持 **Testing**，在 Test users 加入真正登录 Drive 的那个完整邮箱。测试状态下包含本指南 Drive 权限的授权及 refresh token 有七天有效期限制；到期要重新 consent，不是每次启动 Router 都重做。以后长期分发需要单独评估发布和验证要求，不能靠切换“生产”保证永久有效。[G6]

### 5 3 Data Access   数据访问

本版 `router_mac.configure()` 会检查以下两个 scope：

```text
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/drive.readonly
```

`drive.file` 用于创建/修改应用获准的文件。`drive.readonly` 允许查看和下载授权账号可访问的 Drive 文件，是**广泛只读、受限 scope**，不是“仅测试文件夹只读”。这版 Router 用它解决 Mac 应用读取连接器创建的协议对象的障碍；这是当前实现要求，不是所有 Drive 集成的普遍最低权限。[G7,R2]

在“添加或移除范围”里分别添加、更新、保存；不要额外加入完整 `drive` 读写 scope、Gmail scope 或服务账号全域委派。也不要假设保存页面会自动升级已有 token：应用必须实际请求新 scope，用户须重新 consent。[G7]

审批前可让 Gemini 解释广泛只读影响。若不接受，停在这里；需要先开发逐文件选择授权或其他兼容设计，而不是删除本版 scope 检查。

### 5 4 Clients   客户端

创建“Desktop app / 桌面应用”，名称可填 `Dots2Codex Mac`。下载 JSON，只保存到自己的 Mac。不要将 JSON 内容粘到 Gemini/Dots，不要截图显示密钥。[G5]

```text
client-secret.json = 应用的 OAuth 客户端配置
按照已授权流程生成的 authorized-user-bidir.json = 用户凭据
```

本指南主路径是 Mac OAuth + Dots 已连接的 Google 插件，因此 Dots 端不需要你再发 Desktop client 文件或 refresh token。不要因云沙箱 localhost 回调失败而改开公网回调、复制 Mac token，或随意改用另一种设备客户端。

## 6  在 Mac 完成授权和文件夹准备

**【Mac 执行】【必须你确认】**

### 6 1 校验并解压到新目录

保留旧测试环境。使用交付的修复版 Gemini 指南合并包及配套外部 `.sha256` 文件，在 Mac 同一下载目录核对。下面文件名为占位符，请替换为实际下载的校验文件名称；不要使用旧 OneClick v1 ZIP 或其哈希。

```sh
cd "$HOME/Downloads"
shasum -a 256 -c ACTUAL_RELEASE.sha256
```

只有校验返回 OK 后，才解压到此前不存在的新目录。再对照随包文件清单；不要把目录里的自述哈希当作来源真实性证明。

确认代码目录中有 `INSTALL_ROUTER.command`、`START_ROUTER.command`、`remote_transport/router_mac.py`。进入该目录，在 Mac 真实 Terminal 检查：

```sh
python3 --version
command -v codex
codex --version
```

本包要求 Python 3.11+，启动时严格匹配 `codex-cli 0.159.2`。这是先前长会话基线的现场验证版本，不是宣称它永远是最新版本。不同版本先做兼容评审，不改版本检查来绕过。[R2,R4]

### 6 2 已有双向凭据 跳过重新登录

已完成授权者直接进入第 7 节。需要检查时用可选工具，它只输出脱敏检查结果。无 `--folder-id` 时仅核验本地记录，不能证明服务器当前授权；指定 folder 时会刷新 token（必要时）并只读该精确目录的 metadata，仍不证明双向 raw-file/CAS：

```sh
. .venv-router/bin/activate
python3 docs/gemini_setup/tools/mac_google_setup.py check --credentials "$HOME/.config/dots2codex/authorized-user-bidir.json" --folder-id 'YOUR_FOLDER_ID'
```

这需要 `.venv-router` 已安装；首次安装者先做下一小节。独立下载指南包时，工具路径是 `tools/mac_google_setup.py`，不是上面的包内路径。

### 6 3 首次授权 先安装独立依赖

在 Router 包根目录执行，不覆盖已有环境：

```sh
umask 077
python3 -m venv .venv-router
. .venv-router/bin/activate
python3 -m pip install -r requirements-router-mac.txt
mkdir -p "$HOME/.config/dots2codex"
chmod 700 "$HOME/.config/dots2codex"
```

把刚下载的 Desktop JSON 移到 `~/.config/dots2codex/client-secret.json`，先核对文件来源，已有同名文件不要覆盖。目录 0700，配置和授权文件 0600。建议用 Finder 确定具体下载文件，不用通配符盲选“最新”。

```sh
chmod 600 "$HOME/.config/dots2codex/client-secret.json"
```

可选辅助工具会先要求明确接受广泛只读范围，然后使用本机浏览器 loopback 授权，并且拒绝覆盖已有授权文件：

```sh
python3 docs/gemini_setup/tools/mac_google_setup.py authorize --client "$HOME/.config/dots2codex/client-secret.json" --output "$HOME/.config/dots2codex/authorized-user-bidir.json" --ack-drive-readonly
```

工具仅接受 Desktop `installed` 配置；出现 `web` 字段或混合配置会拒绝。它使用 SDK 的 `authorization_prompt_message` 抑制终端授权 URL，并屏蔽本次 SDK 日志；这不代替凭据文件的私有保存。

在自动打开的浏览器选择正确账号，核对应用和权限后由你亲自允许。完成以 Terminal 的 `OAUTH_FILE_SAVED`、`refresh_token_present=true` 为准；浏览器显示完成不替代本地文件保存确认。[G5,G16]

若现有文件需更新授权，使用一个**新的输出文件名**，验证后按同包升级规程审核配置迁移；不要只靠 `--credentials` 覆盖已有配置；不要先删旧 token，也不要将 token 放入 shell 命令或环境变量。

### 6 4 准备专用 transport 文件夹

已有通过双向验证的文件夹可复用。首次应由 Mac 这个 OAuth App 创建，避免纯 `drive.file` 对手动建立文件的授权歧义。辅助工具只在 `--confirm-create` 下新建，且用本地 receipt 防止不明失败后盲重建：

```sh
python3 docs/gemini_setup/tools/mac_google_setup.py create-folder --credentials "$HOME/.config/dots2codex/authorized-user-bidir.json" --name 'Dots2Codex Transport' --receipt "$HOME/.config/dots2codex/folder-create-01.json" --confirm-create
```

记下返回的 `folder_id`。失败时保留 receipt，不换文件名重跑；先检查真实云端状态。不要修改共享权限，不要设成“知道链接的任何人”。

### 6 5 Mac HTTPS 证书

本包在未设置 `SSL_CERT_FILE` 时尝试使用 `certifi`。如仍遇到证书验证失败，在当前虚拟环境核对证书路径：

```sh
export SSL_CERT_FILE="$(python3 -c 'import certifi; print(certifi.where())')"
```

不得用关闭 TLS 验证、`curl -k` 或盲目 `sudo pip` 解决。若有企业 TLS 代理，应向管理员取得批准的根证书方案。[R2,G17]

## 7  第一次安装 Router 三个输入

**【Mac 执行】**

已有授权和文件夹后，双击 `INSTALL_ROUTER.command`。它会创建/复用包内 `.venv-router` 并安装依赖，然后询问：

| 提示 | 输入 |
|---|---|
| Google Drive transport folder ID | 已批准、已核对的真实 folder ID |
| Authorized-user file | Mac 自己的 `authorized-user-bidir.json` 绝对路径 |
| Codex workspace | 新的、非敏感测试目录，例如 `~/Dots2Codex-router-test` |

如需显式指定 Codex 可执行文件，可在 Terminal 使用真实路径：

```sh
bash INSTALL_ROUTER.command --codex /actual/path/to/codex
```

安装器读取 scope 和 folder metadata，不会替你完成 OAuth，也不会通过一次安装就证明全部双向访问。已有目标配置时拒绝静默覆盖；先按同包升级规程确认旧会话权威关闭，再由操作者审核配置迁移，不要直接删除配置或活动指针。成功输出应包括 `configured: true`、`folder_verified: true`、`credentials_copied: false`。[R1,R2]

普通配置写入 `~/.config/dots2codex/router.json`，只有凭据**路径**，没有 token。活动状态为 `router-active.json`。安装器配置成功不代表 Dots 已在线。

若 macOS 安全提示阻止运行，只在核对来源/哈希后按系统提供的单文件批准方式处理。不要全局关闭 Gatekeeper，也不要对整个 Downloads 批量清除隔离属性。

## 8  先让 Dots 具备能力 再按一键配对

**【Dots 执行】【必须你确认】**

Dots 要使用同一份 Router 源码，具备已授权的 Drive/Docs 读写工具、原始文件字节读回方式、私有可写持久目录，以及真实 native worker 接纳能力。它的 Python 不能继承插件 OAuth；Google 工具动作由活跃 Dots 执行。[R3]

先把 `04_DOTS_HANDOFF.zh-CN.md` 交给 Dots。它必须确认真实执行槽位可用，不是只生成一个名称。私有持久 ledger 和 raw-file 读回能力也必须具备。thread limit 或关键能力缺失时停在配对前；HOME 只读可使用明确批准的私有可写路径，不绕过访问限制。

不要让 Gemini Cloud Assist 接收本轮真实 join_code，也不要让 Gemini 冒充 Dots 的 native_task_id。

## 9  日常 启动一次 发一段话

**【Mac 执行 → Router/Dots 自动】**

双击 `START_ROUTER.command`。它会创建新的 Bootstrap Doc 和 Control Doc，复制一段 `DOTS2CODEX_ROUTER_JOIN_V1` 消息到剪贴板。启动器有意在 Mac Terminal 显示并复制此私有消息。只把这段发给已准备好的 Dots，不发群聊、GitHub 或 Gemini 配置会话；发送后清空剪贴板并妥善处理终端记录。Dots 将 code 写入 0600 私有文件，经 `--join-code-file` 读取，不放 CLI 参数或环境变量。

后续应按顺序观察：

```text
WAITING_FOR_WORKER
→ Dots 验证签名根及 Mac → connector 原始字节 probe
→ 平台真实 admission 与签名 WORKER_ADMITTED
→ connector → Mac 反向原始字节 probe 校验通过
→ 创建 4 小时 pin / controller journal
→ 初始化正式 Control Doc
→ BUNDLE_READY
→ 单一私有 runtime 绑定并行模式并验证 control IDLE
→ 签名 WORKER_POLLING
→ CONSUMED
→ 本机 ROUTER_READY
→ Codex 交互窗口
```

默认 Bootstrap 等待期 1800 秒；pin 默认 14400 秒、最多 128 个模型请求，scope 是 `responses_tools`。128 不是 128 个用户任务：一个工具循环可能使用多次模型请求。硬上限为 8 小时及 128 请求；时长只是协议上限，不保证平台让 worker 实际存活四小时。[R2]

Bootstrap Doc 暂存 pin/config 的 base64 字节和校验信息；正式 Control Doc 单独保存执行状态。base64 不是加密。`CONSUMED` 仅移除 Bootstrap **当前正文**里的 bundle，不保证版本历史、备份或已有读取副本清除。[R2,R3,G9]

配对每步都保留同一私有 ledger 和一次性 operation ID。写入响应丢失或读回已被对端推进时，只凭本次完全相同的已签名事件链前缀对账；不能仅凭最新 stage 判断成功。未知 materialize 中断不能换 runtime 再领一份。

Router worker 必须使用同包 `connector_cell claim-begin` 和 `upload-commit`；只有所有原始上传对象验证通过才可结果 CAS。成功 begin 后完整输入所有连续 chunks 必须进入同一实际获接纳的 native context，再真实推理一次。细节见第 04 章及权威 join 规程。

不要手工插入说明、加 tab、切建议模式或 undo 控制文档。哈希/HMAC 不能把不可信编辑者变成可信；所有拥有协议资源写权限的人都在信任边界内。

## 10  首次验收 按最小真实路径测试

**【Mac 执行】【必须你确认结果】**

在同一个 Codex 交互会话先发：

```text
Reply with exactly: ROUTER_TEXT_OK
```

实际看见回答后再发：

```text
Repeat the exact token from your immediately previous answer,
prefixed with PREVIOUS=, and output nothing else.
```

预期是 `PREVIOUS=ROUTER_TEXT_OK`。这只验证本轮历史传递，不宣称所有模型记忆能力已验证。

然后在空白测试 workspace 让它创建一个未存在的小文件，读取并输出确认。只批准该测试目录中的操作；保留 `on-request` 和 `workspace-write`，不要全局关闭审批。[R4,G18]

独立在另一个 Terminal 核对真实文件字节。随后用 `operator status` 找到本轮真实 request/result ID；实际观察后才 `operator ack`。最后一个回答不会仅因为 HTTP flush 就自动成为交付证明。完整命令见 Mac runbook。

验收报告至少记录：实际源码版本/ZIP hash、Codex 版本、双向 probe、两个文本回合、本地工具结果、全部请求确认、CAS close。不能把 READY、模拟测试或一份状态文件当作整个现场测试通过。

## 11  正常结束与故障边界

先停止提交新任务，确认实际结果已经观察和 ACK，再退出 Codex 并运行 `STOP_ROUTER.command`。它先保存 stop intent，等待启动临界区退出，然后直接对正式 Control Doc 提交或对账 CAS close；facade 已死或 ready 缺失都不能跳过权威关闭。[R2]

分别检查 `closed`（正式控制权威已核验）、`process_stopped`（本地进程）、`bootstrap_cleanup`（当前正文清理）及 `worker_stop_confirmed`（平台 worker 停止证据）。缺失、不可读或未知结果进入 `RECOVERY_REQUIRED`，不能报告 `CLOSED`。`worker_stop_confirmed=false` 时 Dots 仍须实际读到 close 并合作停止；close 不取消已经消费的执行。

超时只表示等候未完成；HTTP 504 不等于推理未开始。已有 `DISPATCH_INTENT`、未知写入结果、工具已发出，都不能以改 ID、重建 journal 或重发 prompt“恢复”。迟到结果按原 request ID 找回；不在当前关闭的 pin 上开启新任务。

## 12  交给 Gemini 的简短执行契约

```text
请作为 Google Cloud 配置助手，按这份指南每次只带我完成一步。
你可以把问题路由给当前实际可用的专业子代理，但不要虚构能力。
每步标明：只读/写入、目标项目、所需权限、验收证据。
写入、IAM、计费、OAuth、共享或删除之前等待我的明确确认。
不要索取 token、client secret、完整 OAuth URL 或 Router join_code。
不要编辑 Bootstrap/Control Doc，不创建 pin，不代替 native worker。
区分 Cloud Shell、Mac Terminal 和 Dots 沙箱。
不能完成时报告具体阻塞，不把建议写成已执行。
```

参考来源见 `08_SOURCES.md`；代码行为以同包修复版源码及权威 join/upgrade 规程为准。指南提供操作流程，不代表已经替用户改变了 Google Cloud 配置。
