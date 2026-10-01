# Dots2Codex Router 配置与操作指南

文档版 G2 | 适用代码为 2026-10-01 修复合并版 | 官方重点资料复核日期 2026-10-01

本指南帮助你复用已有 Mac 授权、按需完成 Google Cloud 配置，并把 Router 配对交给真实获接纳的 Dots 执行上下文。Gemini 仅提供配置辅助；新 Router 完整现场验收仍待完成。

## 从已有环境开始

**已经完成 Mac OAuth、双向 Drive 读取和 Codex 0.159.2 验证，直接阅读第 03 章的已有环境路线。不要重建 Cloud 项目、重新 consent 或复用已关闭的 pin。**

首次使用者按第 01 章设置。Cloud Assist 可用时先复制 `PROMPT_GEMINI_START.txt`，再逐段使用第 02 章的 13 段提示词。没有 Cloud Assist 也能手动配置。

## 阅读顺序

- 第 01 章 Google Cloud 与 Mac 首次设置：项目、API、OAuth、安装及最小真实验收
- 第 02 章 Gemini 提示词：P00 至 P12，每次一段，写入前确认
- 第 03 章 Mac 操作手册：已有环境、状态、迟到结果、ACK 与权威关闭
- 第 04 章 Dots 交接：私有配对 ledger、双向 raw probe、并行 cells 与同上下文完整输入
- 第 05 章 故障排查：最小只读检查及未知结果对账
- 第 06 章 安全与验收：权限范围、保留历史、关闭语义及证据等级
- 第 07 章 安装与验收记录：逐项记录 PASS、FAIL 或未验证
- 第 08 章 来源：官方链接、代码依据及版本来源
- 附录 可选设置辅助：`tools/README.zh-CN.md`

`HANDBOOK.pdf`、`HANDBOOK.docx` 与 `ALL_GUIDES.html` 汇总以上全部章节及附录。Markdown 是规范文字源；命令优先从 Markdown 或 HTML 复制，不使用 PDF 视觉换行拼命令。

## 操作分工

**Gemini 可辅助：** 解释页面、检查实际可见状态、提出最小配置计划。工具不可用时给手动步骤，不虚构执行结果。

**必须你确认：** 新建资源、API 启用、IAM、OAuth consent、费用、共享或删除。现有授权不等于批准扩大权限。

**Mac 执行：** 本地 Terminal、独立 Python 环境、浏览器 loopback、官方 Codex。不要搬到 Cloud Shell。

**Dots 执行：** 在已有 Google connector 授权和真实 native admission 下执行配对及请求；Python helper 本身不接纳、唤醒或调用模型。

## 代码版本和证据

文档以修复版源树 `8da5c5ada6c3fa85dc64317653f5f7961b8adf4eed73ae9a44563e8141a4e5c8` 为迁移基线。该值不是本指南增强包的 ZIP 哈希，也不是 Git commit。

最终包的树清单和外部 SHA-256 校验文件是安装依据。完整 ZIP 的最终哈希不写回自身，避免自引用。新包应独立解压，不覆盖旧测试目录或活动 runtime。

历史现场证据、修复基线的离线测试、新迁移包的离线测试和待验收事项分别记录。不要把旧指南的 10 个 Router 测试或旧包哈希当成本包证据；本包实际结果见随包验证文件。
