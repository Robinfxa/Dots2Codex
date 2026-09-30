# Dots2Codex

把未修改的官方 Codex CLI 接到一个已经活跃、获授权的原生推理 worker。
默认仍是有界、单用户、文本模式；Python 只负责传输、状态和回执，不提供模型、
原生任务权限、免费额度或自动唤醒。

现在有两条独立路径：

- **本地共享目录桥接**：CLI 与 loopback facade 在同一环境，通过私有 POSIX 目录与 broker 通信。[完整安装和操作说明](LOCAL_BRIDGE_README.zh-CN.md)
- **实验性远程 Drive + Docs CAS**：Drive API 保存不可变 JSON 消息，固定 Google Doc 的 `requiredRevisionId` 控制 claim、begin、结果和交付回执。不是把共享目录放进 Drive 同步盘

## 当前验证到哪里

已完成一次真实 Drive/Docs 连接器 → 新原生 worker → 控制方接收结果的文本往返，
最终为 `DELIVERED`、控制 epoch 5。五个真实消息对象、hash、依赖链和随机文本挑战均核对。

该测试使用同一个已连接的 Google principal。**尚未完成独立机器上的 Codex CLI +
远程 facade 两轮端到端测试，也没有证明独立 OAuth 客户端与连接器跨 app 的文件互通。**
新提供的 Google SDK 客户端示例仅做了离线测试。launcher/profile/readiness 集成仍需验证；
旧 `desktop_bootstrap.py` 不能直接改个地址就当作远程版使用。

首次真实测试暴露了 Docs 保留末尾换行的差异：严格校验在 native START 前停止。
修正 API 匹配/替换边界后，只手动修复了那一个已知合成空行，再完成后续新 claim、begin、
result、receipt。没有放宽全文 parser，也没有把这次人工修复包装成自动恢复能力。
[公开验证摘要与限制](docs/REMOTE_VALIDATION.md)

## 远程端需要下载什么

每个端点下载**同一固定 commit 的完整仓库**，不要只复制一个 Python 文件：

```sh
git clone https://github.com/Robinfxa/Dots2Codex.git
cd Dots2Codex
git rev-parse HEAD
# 记录并人工核对这个 commit；另一端 checkout 同一个 commit
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-test.txt
# 只有准备使用独立 Google API 客户端时才安装：
python3 -m pip install -r requirements-google-example.txt
```

以上安装须在你允许安装软件的环境执行。使用 Python 3.11+、POSIX 系统；开发验证环境为
Python 3.12。整个仓库的测试与可选本地工具范围需要 `jsonschema` 4.x；远程核心和其原有
离线测试仅用标准库。Google SDK 示例的额外依赖在上面的独立 requirements 文件中。

两端分工：

1. **客户端 / controller 端**：官方 Codex CLI、Python、本仓库、独立授权的 Google API 客户端；CLI 和远程 loopback facade 必须处在可互通 `127.0.0.1` 的同一环境
2. **broker / native worker 端**：本仓库、Python、该端自己的 Google API 授权，以及平台真正支持、已获授权的原生推理上下文。普通 Python daemon 不会因此获得 dot/native 工具或推理能力
3. **两端各自保存**：私有、持久、不可复制/回滚的角色 journal。只可信传递相同 deployment pin 和非凭据配置；绝不复制运行中的 journal、连接器 OAuth 或别的端点 token

Codex 二进制从 [官方项目](https://github.com/openai/codex) 获取。本仓库不打包 CLI、凭据或模型。
本地路径和下载校验的详细说明保留在[原本地安装文档](LOCAL_BRIDGE_README.zh-CN.md)。

## 远程配置顺序

完整可执行命令、已提供的客户端 factory、初始化和停止说明在
[远程端安装配置指南](docs/REMOTE_SETUP.zh-CN.md)。概要如下：

1. 用户自己在 Google Cloud 启用 Drive API / Docs API，创建并批准该端的 OAuth 客户端；登录、同意授权、凭据保存都由用户完成。现有 dot 连接器的授权不能转给 Python
2. 准备专用私有测试 folder 和空白单-tab Google Doc，记录精确 folder ID、document ID、tab ID、control ID。不得按文件名查“最新”文档，也不得让人同时编辑控制文档
3. 审核 `examples/google_clients.py`，明确指定该端已获授权的私有 credential 文件路径。提供了 `create_drive_client` 和 `create_docs_client`，不需要自己猜接口；示例不会替你登录或申请权限
4. 最小权限优先，但读写消息与控制记录需要写权限。`drive.file` 只覆盖该 app 创建或被明确选中/打开的文件；**选中一个文件夹不等于获得另一个 OAuth app 创建的全部子文件**。两端必须验证实际消息 file ID 和控制 Doc 的访问，必要的新增权限需用户另行批准
5. 先做只读 access 检查；在真实原生 worker 已被平台接纳后生成新 pin、各自创建新 journal，显式初始化那份已批准的空白控制 Doc
6. 在每条运行命令上保留完整 Drive + CAS 参数，再启动 facade / worker。只有一次性 begin 许可允许那个真实 worker 处理实际请求；文件到达不会自动唤醒它
7. 看见并核对实际结果后才记 delivery receipt。停止 facade 用 Ctrl-C；不确定的写入、执行或交付都保留现场并核对，不删除 journal、不新建任务重跑

显式选择参数示例（实际 ID 只放端点私有配置，不提交）：

```text
--transport drive --folder-id FOLDER_ID
--client-factory examples.google_clients:create_drive_client
--drive-mode duplicate_tolerant
--docs-client-factory examples.google_clients:create_docs_client
--control-document-id DOCUMENT_ID --control-tab-id TAB_ID
--control-id CONTROL_ID --control-writer-identity WRITER_LABEL
```

`duplicate_tolerant` 允许相同内容的物理副本，不是分布式锁；执行控制由 Docs CAS 和持久 journal
共同约束。`strict_ids` 仍需单独验证预分配 ID 的创建/重试能力。CAS journal 已登记后，省略 CAS
参数会失败关闭，不会悄悄降级到单 writer 模式。

## 测试和文档

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s remote_tests -v
python3 -m unittest discover -s remote_audit/cas -p 'test_*_independent.py' -v
python3 -m unittest discover -s remote_audit/drive -p 'test_*_independent.py' -v
# 安装了 requirements-google-example.txt 后，运行真实 SDK 的离线检查：
python3 -m unittest discover -s sdk_tests -v
python3 -m compileall -q .
# 完全离线、合成结果，不访问 Drive、不调用原生推理：
python3 -m remote_transport.demo --root /tmp/dots-drive-demo-UNIQUE
```

- [远程安装配置](docs/REMOTE_SETUP.zh-CN.md)、[远程验证摘要](docs/REMOTE_VALIDATION.md)
- [Drive 设计](DRIVE_DESIGN.md)、[普通单 writer 模式](DRIVE_RUNBOOK.md)、[Docs CAS 细节](DOCS_CAS_RUNBOOK.md)
- [原本地 README](LOCAL_BRIDGE_README.zh-CN.md)、[本地自动部署提示词](docs/AUTO_DEPLOY.zh-CN.md)、[本地验证摘要](docs/VALIDATION.md)
- [会话固定分配](ROUTING_RUNBOOK.md)、[固定 nonce 工具验证](TOOL_PROBE.md)、[有界仓库研究](REPO_REVIEW_RUNBOOK.md)
- [安全边界](SECURITY.md)、[角色契约](ROLE_CONTRACT.json)

已有 `text_only`、`tool_probe`、`repo_review` 范围与本地实现保留。先前仓库研究的真实取数仍为
`network_unavailable`，不能用新增 Drive 测试替代那项验证。远程模式只输出文本，不支持一般工具循环、
自动接管、无人值守调度或 exactly-once 外部副作用。

## English summary

An experimental bridge from the unmodified official Codex CLI to an already active,
authorized native inference worker. The existing local POSIX bridge is preserved.
The new independent transport uses immutable Drive blobs plus a pinned Docs revision-CAS
control record. One same-principal connector/native text roundtrip was observed;
independent-machine CLI integration, separate OAuth/app interoperability, unattended
wake and exactly-once external effects remain unverified. No credentials, model
backend, permanent scheduler or free inference entitlement is bundled.
