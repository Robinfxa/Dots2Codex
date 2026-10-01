# 官方来源与版本依据

原指南资料核验日期为 2026-09-30；本次于 2026-10-01 重新核对 G1、G3、G4、G6、G7、G10、G16 的重点主张。其他官方链接保留供部署时复核。Google 页面、IAM、配额和 UI 可能变化；后续以真实界面和当日官方来源复核。以下链接用于来源追溯，不是授权用户资源的链接。

## Google 官方资料

- **G1** 使用 Cloud Assist 面板、页面上下文、会话历史和能力限制：[官方页面](https://docs.cloud.google.com/cloud-assist/chat-panel)
- **G2** Cloud Assist 项目级设置、所需/可选 API：[官方页面](https://docs.cloud.google.com/cloud-assist/set-up-gemini)
- **G3** Cloud Assist IAM 与“用户权限相同”的边界：[官方页面](https://docs.cloud.google.com/cloud-assist/iam-requirements)
- **G4** Cloud Assist MCP Root Agent、专业子代理和 HITL（MCP 接口为私有预览）：[官方页面](https://docs.cloud.google.com/cloud-assist/reference/mcp)
- **G5** Drive Python quickstart：Cloud 项目、Desktop OAuth、客户端库；其示例 scope 不是本项目的完整要求：[官方页面](https://developers.google.com/workspace/drive/api/quickstart/python)
- **G6** Audience、Internal/External、Testing 与七天授权：[官方页面](https://support.google.com/cloud/answer/15549945)
- **G7** Drive scopes、受限权限与逐文件授权：[官方页面](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- **G8** Drive 错误：404 不存在/不可访问，403/429：[官方页面](https://developers.google.com/workspace/drive/api/guides/handle-errors)
- **G9** Docs 历史版本：[官方页面](https://support.google.com/docs/answer/190843)
- **G10** Drive 配额与费用：[官方页面](https://developers.google.com/workspace/drive/api/guides/limits)
- **G11** Docs 配额：[官方页面](https://developers.google.com/workspace/docs/api/limits)
- **G12** Docs batchUpdate / requiredRevisionId / 原子请求：[官方页面](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- **G13** Docs 读取及可用授权范围：[官方页面](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get)
- **G14** Cloud Assist 产品页及预览价格说明：[官方页面](https://cloud.google.com/products/gemini/cloud-assist)
- **G15** gcloud services enable：[官方页面](https://docs.cloud.google.com/sdk/gcloud/reference/services/enable)
- **G16** Google auth-oauthlib 官方库文档 InstalledAppFlow：[官方页面](https://google-auth-oauthlib.readthedocs.io/en/latest/reference/google_auth_oauthlib.flow.html)
- **G17** Python 官方 macOS 安装/环境说明：[官方页面](https://docs.python.org/3.11/using/mac.html)
- **G18** 官方 Codex CLI 命令参考（当前站点可能重定向；本包仍按 0.159.2 验收）：[官方页面](https://developers.openai.com/codex/cli/reference)

## 修复版代码依据

迁移基线包是 `Dots2Codex-OneClick-Router-Merged-20261001-8da5c5ada6c3`，其 public-source tree SHA256 为：

```text
8da5c5ada6c3fa85dc64317653f5f7961b8adf4eed73ae9a44563e8141a4e5c8
```

此值是基线源树摘要，不是 Git commit 或最终指南增强 ZIP 的摘要。最终包以随包 manifest 和外部 `.sha256` 为准；正文不嵌入自己的最终 ZIP hash。旧 OneClick v1 ZIP 仅为历史来源，不应再次安装覆盖当前实现。

- **R1** 根目录四个 .command、README_ROUTER.txt 与 mac_router 入口
- **R2** remote_transport/router_mac.py：配置、启动生命周期、status 和直接 authoritative close
- **R3** remote_transport/router_bootstrap.py、router_join.py、docs/ROUTER_JOIN_V1.zh-CN.md：内部 V2 签名链、ledger、双向 probe、单 runtime 和精确绑定
- **R4** remote_transport/operator.py：Codex 入口、status、result 与 ACK
- **R5** docs/ACTIVE_CONNECTOR_WORKER.md、connector_worker.py、connector_cell.py、connector_files.py 及 native_connector：并行 cells、原字节验证与完整输入分块
- **R6** examples/google_clients.py、drive_http.py、backend.py，以及本指南的 tools/mac_google_setup.py
- **R7** docs/ROUTER_VALIDATION.md、docs/ROUTER_UPGRADE.zh-CN.md、对应 Python 与 Node 测试
- **R8** 本指南 verification 下的测试日志、GUIDE_BUILD_VERIFICATION.json 及最终发布验证摘要

## SDK 兼容性补充来源

Google 官方仓库的 [google-auth-oauthlib 1.2.2 flow.py](https://github.com/googleapis/google-auth-library-python-oauthlib/blob/v1.2.2/google_auth_oauthlib/flow.py) 用于核对 requirements 固定版的 loopback 参数。当前 helper 使用 `authorization_prompt_message`、显式 PKCE、127.0.0.1 随机端口和 300 秒超时，拒绝混合 web 配置。

本轮 mock SDK 执行环境为 1.5.0；1.2.2 是官方源码兼容性核对，未安装或运行。离线 mock 通过不等于真实 OAuth consent 成功。

## 验证披露

本次只进行了本地代码和文档迁移、离线测试及格式检查，没有 Google 云写、OAuth 登录、真实 native inference 或 GitHub 发布。helper 操作必须由用户在 Mac 明确批准后手动运行，不由 Router 自动调用。

历史四请求与真实 Mac 工具循环属于原长会话基线。新 Router 自动配对、并行路径、Mac 故障恢复和多小时稳定性仍需现场验收；不要把旧指南的 10 测试记录或基线总数误作本次包的重新执行结果。

规范文字源为 00 至 08 的 Markdown 加 tools/README.zh-CN.md；P00 同步生成独立启动 TXT。HTML、Word 和 PDF 从同一规范文字生成，当前构建与渲染记录见 GUIDE_BUILD_VERIFICATION.json。文档无真实用户账号、凭据、join_code 或私有运行资源 ID。
