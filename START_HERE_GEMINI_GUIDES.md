# Google Cloud 和 Gemini 辅助设置入口

本指南 G2 适用于 2026-10-01 修复合并版 Router。它保留新版长会话、工具续轮、并行 connector cells 和完整输入分块路径；不要安装旧 OneClick v1 包覆盖它。

**你已完成 Mac 双向 OAuth、专用 Drive 文件夹和 Codex 0.159.2 测试：直接从 [已有环境](docs/gemini_setup/03_MAC_ROUTER_RUNBOOK.zh-CN.md) 开始。复用经批准的本机凭据与文件夹，不重复建项目或授权。**

首次设置可读 [完整离线指南](docs/gemini_setup/ALL_GUIDES.html)、[PDF 手册](docs/gemini_setup/HANDBOOK.pdf) 或 [Word 手册](docs/gemini_setup/HANDBOOK.docx)。所有版本源自同一组 Markdown；执行命令优先从 Markdown 或 HTML 复制。

需要 Gemini 帮忙时，把 [启动提示词](docs/gemini_setup/PROMPT_GEMINI_START.txt) 发给 Cloud Console 中实际可用的 Gemini Cloud Assist，再按 [13 段提示词](docs/gemini_setup/02_GEMINI_PROMPTS.zh-CN.md) 操作。变更逐项由你确认；不要发送凭据、join_code 或用户任务正文。

四个 Mac 入口：`INSTALL_ROUTER.command`、`START_ROUTER.command`、`ROUTER_STATUS.command`、`STOP_ROUTER.command`。可选 OAuth 工具不会随这些入口自动运行。Dots 必须使用同包 [权威配对规程](docs/ROUTER_JOIN_V1.zh-CN.md)，旧 helper 不兼容内部 V2 协议。

**验收边界：** 原长会话基线有四个真实模型请求及 Mac 工具循环记录；新 Router 自动配对、并行路径、修复版设置辅助仅有离线验证，真实部署仍待验收。默认 4 小时及 128 请求，硬上限 8 小时及 128 请求，不承诺 native worker 自动唤醒或持续存活。详见 [安全与验收](docs/gemini_setup/06_SECURITY_AND_VALIDATION.zh-CN.md)。
