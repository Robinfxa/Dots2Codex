# 远程端安装与配置：Drive + Docs CAS

这是实验性部署指南，包含可下载的客户端示例和操作命令。当前通过的是同一 Google principal
的连接器/native 文本往返，以及离线 HTTP/SSE 测试；不是开箱即用的远程 Codex 产品。
独立 OAuth/app 文件访问、真实 Codex CLI 的 profile/readiness/两轮回执仍是上线前门槛。

## 1. 两端下载同一版本

从 README 的 clone/venv 命令开始。在第一端记录完整 commit SHA，然后在第二端执行：

```sh
git clone https://github.com/Robinfxa/Dots2Codex.git
cd Dots2Codex
# VERIFIED_COMMIT 是你核对过的本次发布 commit，不是下面的字面占位符
git checkout --detach VERIFIED_COMMIT
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-test.txt
python3 -m pip install -r requirements-google-example.txt
```

Python 3.11+、POSIX、可用的 flock/fsync/原子 rename 和私有本地存储是前提；测试使用 Python
3.12。不要把 journal 放在 Drive Desktop 同步目录、共享网络盘或复制到第二个活动主机。
下载完整仓库：remote facade 还使用已有 vendor HTTP/SSE 代码，不能只复制 remote_transport。

客户端端安装来自[官方项目](https://github.com/openai/codex)的 Codex CLI。broker 端必须由实际平台
接纳一个获授权的原生推理 worker；本仓库不下载或模拟这个权限，也没有永久后台 broker 安装器。

## 2. 用户完成端点自己的 Google 授权

先完成这些**用户操作**，不要把密码、OAuth token、client secret 发到聊天或提交到仓库：

1. 在自己的 Google Cloud 项目启用 Drive API 和 Docs API，配置 consent screen 和适用的 OAuth client
2. 用户在该端点完成登录/同意，明确批准所需范围与凭据保存位置；每个端点独立授权，不复制连接器/browser/另一个端点的 token
3. 如果采用本仓库示例，准备 Google 官方 `Credentials` 支持的 authorized-user JSON，保存在仓库外的私有目录，目录 0700、文件 0600，且只归当前用户所有
4. 该文件是用户为这个集成已经批准的凭据。factory 只在被显式调用时读取你指定的唯一文件；不搜索 ADC、浏览器、dot、系统钥匙串或其他 app 的认证缓存，不启动 OAuth，也不保存刷新后的 token

[Google Python quickstart](https://developers.google.com/workspace/docs/api/quickstart/python)说明了项目、
OAuth Desktop client 和用户本机同意流程，但其只读 sample scope 不能直接用于本协议写入。
根据下面的范围说明调整你自己的授权流程，别盲目沿用 quickstart 的 sample document、目录或
删除 token 的建议。该 quickstart 面向测试环境；生产凭据设计需另行审核。

```sh
# 只指定文件路径；不要把 token 本身写进 shell 命令或环境变量
export DOTS_GOOGLE_AUTHORIZED_USER_FILE="$HOME/.config/dots2codex/authorized-user.json"
# 只有在你确认该文件就是你为本集成准备的文件后，才设置权限
chmod 600 "$DOTS_GOOGLE_AUTHORIZED_USER_FILE"
```

依赖见 `requirements-google-example.txt`。工厂入口已经提供：

- `examples.google_clients:create_drive_client`：官方 Google auth 刷新既有 grant，接入现有 `DriveHTTPClient`
- `examples.google_clients:create_docs_client`：官方 Docs Python SDK，full-tab / inline-suggestions 读取，原样传递 `requiredRevisionId`

示例不申请新 scope，关闭 SDK 状态重试和授权失败重放；底层 httplib2 在特定 socket
失败时仍可能重发完全相同的请求。相同 requiredRevisionId 限制其只能成功提交一次；若首个
成功响应丢失而重发返回 stale，结果仍按未知处理，不再发 native permit。Docs 400、timeout 等错误都保守视为未知，
不会仅凭 HTTP 400 猜成 stale-revision 后重跑。错误输出不含 API 原始内容或秘密。
这两个 standalone factory 未做真实 Google API 集成测试，不能用先前连接器测试替代。

### 最小权限与跨 app 限制

- controller 和 worker 都要上传本角色消息、读取对方消息、更新同一个控制 Doc。只读权限仅适合观察者，`drive.readonly` 或 `documents.readonly` 不足以运行协议
- 优先评估 `https://www.googleapis.com/auth/drive.file`。Docs get/batchUpdate 也接受这个 scope，但目标文件必须确实已向这个 OAuth app 授权
- `drive.file` 针对 app 创建或用户通过 app/Picker 明确选择/打开的文件。**共享或选择父文件夹不保证该 app 能读取其他 app 创建的全部子文件**。同一个 Google 账号也不等于同一个 OAuth app
- 现有连接器 app 与新 Desktop OAuth app 不可假设互通；必须在两端实际读取对方新建的测试消息 file ID、同一控制 Doc 和精确 folder 元数据。若不能满足，先停在部署门槛，审核同 app 创建/文件选择接入或用户另行批准的适用 scope
- `documents` 只解决 Docs 权限，不能代替 Drive blob 权限；广泛的 `drive` 是受限制的大范围授权，不能为了省事自动申请。文件 ACL 和 OAuth scope 两层都必须满足

官方依据：[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)、
[Docs get scopes](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get)、
[Docs batchUpdate scopes](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)。

## 3. 固定资源与只读检查

用户准备一份专用私有测试 folder 和一份全新的空白、单 tab Google Doc；不要使用工作文档。
不得有其他编辑者并发修改、建议、额外 tab 或人工 undo。记录实际 ID，不按名称搜索“最新”文件。
两端使用同一 folder/document/tab/control ID，但各自使用与授权账号对应的 writer label。
writer label 只是本地绑定标签，不是认证或 Google 账号验证。

```sh
export FOLDER_ID='REPLACE_WITH_APPROVED_FOLDER_ID'
export DOCUMENT_ID='REPLACE_WITH_APPROVED_DOCUMENT_ID'
export TAB_ID='REPLACE_WITH_ACTUAL_TAB_ID'
export CONTROL_ID='REPLACE_WITH_NEW_UNIQUE_CONTROL_ID'
export WRITER_LABEL='REPLACE_WITH_THIS_ENDPOINT_WRITER_LABEL'
```

tab ID 必须来自实际 Docs API 返回，不要假定为 `t.0`。如需读取，可在私有终端运行：

```sh
python3 - <<'PY'
import os
from examples.google_clients import create_docs_client
value = create_docs_client().get_document(os.environ['DOCUMENT_ID'])
print([tab['tabProperties']['tabId'] for tab in value.get('tabs', [])])
PY
python3 -m examples.remote_setup check-access --folder-id "$FOLDER_ID" \
  --document-id "$DOCUMENT_ID" --tab-id "$TAB_ID"
```

检查只证明配置对象可读取、tab 存在、Doc 有编辑 revision；不会上传或测试 CAS 写入，
也没有证明跨 app 新消息可见。folder 的确切类型和 ACL 由用户在 Drive 中确认。
只读检查成功之后，仍要在获批准的专用资源中单独通过“各端上传一条合成消息 → 对端按
实际返回 ID 读原始字节 → hash 相符”以及 CAS 测试；未经验证不得开始处理真实请求。

## 4. 新会话 pin 与独立 journal

先完成授权、访问测试与 worker 接纳，再创建短生命周期 pin；寿命从创建时就开始计时。
不要把构建环境里的历史 ID 当作你的原生任务 ID。真实平台必须已经返回该 worker 的身份。
controller 私有终端：

```sh
umask 077
export RUN="$HOME/.local/state/dots2codex/run-UNIQUE"
mkdir -p "$(dirname "$RUN")"
mkdir -m 700 "$RUN"  # 必须全新，已有目录立即停止
export NATIVE_TASK_ID='REPLACE_WITH_ACTUAL_ADMITTED_TASK_ID'
python3 -m remote_transport.cli new-deployment --pin "$RUN/pin.json" \
  --session remote-test-1 --native-task-id "$NATIVE_TASK_ID" --seconds 900
python3 -m remote_transport.cli provision-journal --pin "$RUN/pin.json" \
  --journal "$RUN/controller" --role controller
```

通过你批准的可信方式把**完全相同的 pin.json**和非秘密 ID 配置交给已接纳的 worker，
在对方主机的全新 0700 目录中保存为 0600，并核对 SHA-256。不要传请求正文、OAuth 或 journal。
worker 使用自己的 RUN 本地路径执行：

```sh
python3 -m remote_transport.cli provision-journal --pin "$RUN/pin.json" \
  --journal "$RUN/worker" --role worker
```

这里只传同一个 pin，并不共享文件系统。绝不为恢复丢失状态而重新 provision，也不复制/回滚 journal。

## 5. 显式初始化已批准的空白控制 Doc

下一条是**真实 Docs 写入**，仅由 controller 对前面已批准的专用空白 Doc 运行一次：

```sh
python3 -m examples.remote_setup init-control --folder-id "$FOLDER_ID" \
  --document-id "$DOCUMENT_ID" --tab-id "$TAB_ID" --pin "$RUN/pin.json" \
  --control-id "$CONTROL_ID" --writer-identity "$WRITER_LABEL"
```

helper 要求全文恰好只有 Docs 的一个末尾换行；在 fresh requiredRevisionId 下插入 canonical block，
保留 Docs 自带末尾换行，然后全文回读核对 IDLE 和 pin。不是空白文档就拒绝，不删除旧内容，
不自动修复，不授予 native execution。若响应丢失，保留现场并人工核对同一 pin/block；不要重试初始化、
新建 pin 或删除控制文档来绕过不确定结果。

## 6. 两端完整参数，启动和处理

下面使用 Bash 数组，分别在两端配置。保持每次命令的所有 CAS 参数完全一致：

```sh
COMMON=(--pin "$RUN/pin.json" --transport drive --folder-id "$FOLDER_ID"
  --client-factory examples.google_clients:create_drive_client
  --drive-mode duplicate_tolerant
  --docs-client-factory examples.google_clients:create_docs_client
  --control-document-id "$DOCUMENT_ID" --control-tab-id "$TAB_ID"
  --control-id "$CONTROL_ID" --control-writer-identity "$WRITER_LABEL")
```

controller：

```sh
python3 -m remote_transport.cli publish-deployment "${COMMON[@]}" --journal "$RUN/controller"
python3 -m remote_transport.cli serve "${COMMON[@]}" --journal "$RUN/controller" \
  --port 0 --deadline 60 --ready "$RUN/ready.json"
```

`serve` 输出真实 loopback base_url 并持续运行到 pin 到期或 Ctrl-C。ready.json 是该进程自己的
状态文件，不是旧 launcher 的 readiness 握手。不要再起第二个 controller/facade。
`duplicate_tolerant` 在未知上传的显式恢复中可能产生相同内容的物理副本；hash 用于完整性和去重，
没有抢锁能力。CAS 仍是必须项。`strict_ids` 需另行验证 generateIds/create-by-ID 的真实服务行为。

已接纳的 worker 在自己的原生上下文内运行：

```sh
python3 -m remote_transport.cli worker-next "${COMMON[@]}" --journal "$RUN/worker" \
  --save "$RUN/permit-1.json" --wait 20
```

pending 不是未执行证明；这个命令不调用模型。仅在返回新的一次性 permit 后，那个已固定的
原生 worker 才能读取它所包含的实际请求并进行一次文本推理。不要把 permit 当作可重启任务。
worker 用实际回答创建新的私有 result 文件 `{"text":"实际回答"}`，然后：

```sh
python3 -m remote_transport.cli worker-complete "${COMMON[@]}" --journal "$RUN/worker" \
  --permit "$RUN/permit-1.json" --result "$RUN/result-1.json"
```

后续请求需上一条的真实 delivery receipt，并使用新的 permit/result 文件名。最多 3 条请求、
900 秒、同一个固定 worker；没有自动换人或恢复不明执行。

### 真实 Codex CLI 接入仍是必须完成的门槛

远程 facade 的目标接口为 `POST /v1/responses`、`model=native-subagent-bridge`、`stream=true`，
必须有 canonical `session-id` 和 UUID `thread-id`。只绑定 loopback；Origin/Authorization header
会被拒绝。它返回文本，不接受工具执行 input/output。

目前没有经过真实 CLI 验证的远程 profile/launcher 模板，因此不要直接用旧
`desktop_bootstrap.py`，也不要把它的 readiness 成功视为此 facade 的成功。
未来接入时需审核实际官方 CLI 版本如何发送这些 header、配置本地 provider，以及下一轮是否
原样回送上一条 assistant item（包括 ID/content）。下一轮 ACK gate 只在合成 HTTP 中验证；
若 CLI 改写或省略该 item，协议会停止，需人工确认交付。不得通过关闭 sandbox/approvals 或
伪造 worker/回执绕过门槛。先在专用合成会话完成两个真实 CLI 回合，核对实际结果及两个回执。

## 7. 停止、观察交付和恢复

在 controller 终端用 Ctrl-C 停止 facade，确认已退出。已发出的请求不会因 HTTP 504、
断开或停止 facade 而取消，worker 仍可能在 pin 到期前处理；必须同时通知实际 worker 停止
接收新任务，并由平台核对它的状态。不要仅凭停止本地进程宣称 native 已取消。

只有已经在真实客户端看见并核对最终回答时，才用其实际 request object ID：

```sh
export REQUEST_ID='REPLACE_WITH_ACTUAL_REQUEST_OBJECT_ID'
python3 -m remote_transport.cli ack-delivery "${COMMON[@]}" --journal "$RUN/controller" \
  --request-id "$REQUEST_ID" --evidence 'Actual final result observed and checked in client'
python3 -m remote_transport.cli status "${COMMON[@]}" --journal "$RUN/controller" --role controller
```

`status` 只做读取/对账概览，不等于 native 调用或客户端交付证明；control Doc 的对应请求应为
DELIVERED，receipt 应指向你核对的 request/result。socket flush、文件出现和人工声明都不能
替代实际观察。保留两端日志、精确对象字节及 control 回读，勿提交公开仓库。

- 读失败或 403/429：保留状态，核对授权或退避再读
- CAS/上传响应不明：对账同一 operation/固定字节；不要改 ID、重建 journal 或重新推理
- 已 begin、缺结果、丢 permit、主机崩溃：停止，核对真实原生执行；没有自动重跑/接管
- 未观察到交付：不 ACK、不开启下一条；不得把重发请求当作恢复
- pin 到期：禁止新请求/新 permit；已知结果与实际回执仍可补记。新会话不能“解决”旧会话的不明结果

详细状态转换见 [Docs CAS runbook](../DOCS_CAS_RUNBOOK.md)。本指南没有进行账号设置、授权、
机器安装或真实资源写入；运行这些示例前仍需各项实际权限和部署验证。
