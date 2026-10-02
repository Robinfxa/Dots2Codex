# Codex 客户端：仅本次会话的 Direct provider

优先用仓库根目录 `./DIRECT.command codex`，它从确认过的本地 route 生成参数。下面只用于审阅；provider ID 占位符需替换为启动器为本轮 route 生成的唯一值，不应直接复制运行。端口和 model / effort 必须与 route 完全一致，工作目录由你在启动时明确选择。`DOTS_BRIDGE_HTTP_BEARER` 在用户终端运行环境中提供，值不出现在命令行。

```sh
codex --no-daemon --sandbox read-only \
  -C /absolute/path/to/approved/workspace \
  -m gpt-6-astra \
  -c 'model_reasoning_effort="xhigh"' \
  -c 'model_provider="dots_direct_REPLACE_WITH_ROUTE_HASH"' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.name="Dots2Codex Direct"' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.base_url="http://127.0.0.1:18765/v1"' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.env_key="DOTS_BRIDGE_HTTP_BEARER"' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.wire_api="responses"' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.requires_openai_auth=false' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.supports_websockets=false' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.request_max_retries=0' \
  -c 'model_providers.dots_direct_REPLACE_WITH_ROUTE_HASH.stream_max_retries=0' \
  -c 'web_search="disabled"'
```

适用版本固定为 **`codex-cli 0.159.2`**。先 `codex --version`，其他版本须独立验收，不自动升级/降级。启动器使用 route 摘要生成唯一 provider ID，以避免沿用已有同名 provider 的 auth/header/catalog 设置。这些 dotted overrides 不承担清空既有 provider 表的作用。命令行 `-c` 是本次配置覆盖；`--no-daemon` 避免复用已运行的共享后台服务。不要为这个实验使用 `--oss`、`--search`、旧 profile、resume/fork 或绕过审批/sandbox 的参数。

`requires_openai_auth=false` 仅表示这个 localhost provider 不使用 Codex 的 OpenAI 登录机制；`env_key` 仍要求本地 bearer，且隧道与真实原生模型各自仍需获准访问。未提供 bearer 会失败，不能把任何账号 key 暴露给普通本机端口。关闭自动 HTTP/stream 重试是这次保守试运行设置；人工重试仍须核对既有动作状态。

本 facade 只有 HTTP `POST /v1/responses` / `POST /responses`，返回 SSE；没有 `/models`、`/responses/compact`、WebSocket 或 hosted 工具服务。支持文字及所广告的 function/custom 工具。截图、文件/音频输入、托管工具、客户端额外压缩流程及任意更长会话不在验收范围。若客户端访问其他 endpoint 或发送不支持的项目，停下核对，不应把错误当作模型响应。

## 首条请求前误退出：仅限零请求的一次恢复

如果旧 Codex 已经完全退出，且从未发送请求，可用 `./DIRECT.command codex-reopen-unused`。不要删除 `codex-started.json`、数据库或任何恢复记录，也不要重新 setup、init、换 route 或修改到期时间。

1. 先让原生控制器暂停，确认它从未收到请求。在原隧道 Terminal 按 Ctrl-C，等隧道及 bridge 子进程完全退出；保留该 Terminal 和它的环境变量
2. 在原 Codex Terminal 运行 `./DIRECT.command codex-reopen-unused`，阅读并输入 `REOPEN`。旧标记没有 PID，旧客户端退出及控制器暂停仍需要你明确确认；进程检查只是补充检查
3. 程序独占空闲端口并只读核对实际 SQLite（包括 WAL）：必须完全没有任何请求、执行/结果记录或 schema 活动，且原授权、绑定、有效期全部匹配。它保留旧标记，新增一次性恢复准备记录
4. 看到提示后，在原隧道 Terminal 用原来的 `./DIRECT.command run` 和同一个已批准的 profile 重启。不要重新 setup/init，不要启动第二个 Codex 或发测试 HTTP 请求。保持原生控制器暂停
5. 确认原隧道/bridge 已健康后，回到恢复窗口输入 `READY`。程序再次核对零活动、原配置和授权，保存消费记录，再以相同工作目录及参数启动 Codex。之后由原生控制器继续同一试用

任何 queued、claimed、completed、cancelled 请求都会拒绝恢复。缺失、损坏、更换、过期或无法确定的状态同样拒绝。准备记录建立后的取消、失败或不确定结果都会保留这次尝试，不能再通过该入口重试。暂停并完整退出旧 bridge 是隔离旧 HTTP 请求的必要步骤；最后检查是单拥有者流程中的只读快照，不是运行中服务器的原子锁或旧客户端身份认证。

## 桌面 App 仅作另行手工验收

`DIRECT.command` 不修改全局 `config.toml`，不替换桌面 App 的 provider，也不重启 App。先完成 CLI 闭环。只有你明确希望另做桌面试用，才在备份和审阅准确配置差异后，由你确认写入所选客户端实际读取的配置，并完整退出/重开对应 App、app-server 或 daemon。

项目 `.codex/config.toml` 不能可靠用来替换这些 provider 设置；当前官方文档说明 provider 等敏感键在项目配置层会被忽略。GUI 进程也不保证继承 Terminal 中的 bearer。不能因为 CLI 正常，就宣称桌面新线程、旧线程或云端任务已经接管。

## 依据与验证状态

- [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)：provider 字段、环境变量认证、传输及 `web_search`
- [官方 app-server 示例](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)：同组 `-c` provider 字段与 Bearer header 的语义；该示例的 OAuth 用途不应照搬为本地凭据
- 固定版本源码：`openai/codex` 的 `rust-v0.159.2` / `ff6aec96948b70d94983af2641a6b67c94faeff5`，本次核对 `model-provider-info`、`model-provider` 和 CLI 参数实现；本地已有二进制帮助输出也确认 `-c`、`--no-daemon`、`-C` 和 `--sandbox`

以上是文档/源码/参数核对；实际 0.159.2 的隔离 `features list` 命令也成功加载了所列 provider 参数。实际 CLI→bridge 请求在本次云端 sandbox 的客户端启动检查处阻止，未到 HTTP，未通过真实 CLI 端到端测试。Mac、隧道、原生模型与桌面 App 仍待现场验收。
