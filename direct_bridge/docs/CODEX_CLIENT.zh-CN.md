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

## 桌面 App 仅作另行手工验收

`DIRECT.command` 不修改全局 `config.toml`，不替换桌面 App 的 provider，也不重启 App。先完成 CLI 闭环。只有你明确希望另做桌面试用，才在备份和审阅准确配置差异后，由你确认写入所选客户端实际读取的配置，并完整退出/重开对应 App、app-server 或 daemon。

项目 `.codex/config.toml` 不能可靠用来替换这些 provider 设置；当前官方文档说明 provider 等敏感键在项目配置层会被忽略。GUI 进程也不保证继承 Terminal 中的 bearer。不能因为 CLI 正常，就宣称桌面新线程、旧线程或云端任务已经接管。

## 依据与验证状态

- [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)：provider 字段、环境变量认证、传输及 `web_search`
- [官方 app-server 示例](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)：同组 `-c` provider 字段与 Bearer header 的语义；该示例的 OAuth 用途不应照搬为本地凭据
- 固定版本源码：`openai/codex` 的 `rust-v0.159.2` / `ff6aec96948b70d94983af2641a6b67c94faeff5`，本次核对 `model-provider-info`、`model-provider` 和 CLI 参数实现；本地已有二进制帮助输出也确认 `-c`、`--no-daemon`、`-C` 和 `--sandbox`

以上是文档/源码/参数核对；实际 0.159.2 的隔离 `features list` 命令也成功加载了所列 provider 参数。实际 CLI→bridge 请求在本次云端 sandbox 的客户端启动检查处阻止，未到 HTTP，未通过真实 CLI 端到端测试。Mac、隧道、原生模型与桌面 App 仍待现场验收。
