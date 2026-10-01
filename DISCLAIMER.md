# 免责声明 / Disclaimer

## 中文

### 项目性质与关联关系

Dots2Codex 是独立的、实验性的社区项目，用于研究和验证已获授权的客户端、传输层与原生
推理 worker 之间的桥接流程。它不是 OpenAI 或 Google 的官方产品，与这些公司不存在
由本项目所声称的隶属、合作、认证或背书关系。文中产品名称和商标属于各自权利人。

### 使用责任与服务限制

使用者应自行确认有权访问相关账号、数据、机器、API、连接器和原生执行环境，并遵守
适用的法律、服务条款、使用政策、许可及组织要求。费用、API 配额、订阅限制、账号状态、
授权范围及其后果由实际操作者核实和管理。本项目不提供模型或推理额度，不赋予额外权限，
也不用于绕过服务条款、平台限制、安全控制或计费规则。

### 实验软件与验证范围

本项目按 [MIT License](LICENSE) 所载条件提供，不作可用性、持续在线、兼容性、无错误、
安全性或特定用途适用性的保证。CAS、签名、journal 和回执只能在文档列明的假设与边界内
提供控制与证据，不能保证所有外部副作用 exactly-once，也不能保证原生 worker 持续存活。

离线测试、模拟 benchmark 和历史真实测试各有独立适用范围。历史上完成的四请求工具循环
不等于新 Router、并行执行器或可选设置工具已经通过真实部署验收；默认四小时和硬上限
八小时是配置边界，不是多小时稳定性承诺。具体证据与未验证项目以
[Router 验证说明](docs/ROUTER_VALIDATION.md)、[迁移验证说明](docs/GUIDES_MIGRATION_VALIDATION.md)
及[历史验证记录](docs/REMOTE_VALIDATION.md)为准。

### 数据、凭据与故障处理

- 各端使用本人明确批准的账号和凭据；不要共享 OAuth 文件、refresh token、client secret 或其他秘密
- 新授权、扩大权限、保存凭据和云写入应逐项审阅并取得所需批准。`drive.readonly` 是较广的 Drive
  只读权限，不应描述为只访问某个专用 folder；最小必要授权优先
- 只向授权参与方开放传输 folder 与 Docs。任务正文、结果、配对信息和历史版本可能留在第三方服务；
  HMAC 不提供加密，清空当前 Doc 正文不删除其历史版本
- 对未知创建、写入、执行或交付保留现场并停止自动重放，以操作 ID 和精确证据核对；
  不删除 journal、换 receipt 或重建任务来掩盖不确定性
- 不应在未经独立安全、隐私、可靠性及适用性评估的情况下用于关键生产或其他高后果场景

请一并阅读[安全边界](SECURITY.md)和[升级与现场验收](docs/ROUTER_UPGRADE.zh-CN.md)。
本说明用于澄清项目边界，不是法律意见，不新增或替代 [MIT License](LICENSE)
及[第三方许可说明](THIRD_PARTY_NOTICES.md)，也不改变第三方服务的条款。

## English

Dots2Codex is an independent, experimental community project. It is not an official
OpenAI or Google product and does not claim affiliation, certification, sponsorship,
or endorsement by either company. Product names and trademarks belong to their owners.

Operators are responsible for verifying their authority to access accounts, data,
devices, APIs, connectors, and native execution environments; reviewing permissions;
managing costs and quotas; and following applicable laws, licenses, organizational
requirements, and provider terms and policies. This project supplies no model access,
free inference entitlement, additional permissions, or means to bypass platform,
security, billing, or contractual restrictions.

The software is provided under the existing [MIT License](LICENSE), without guarantees
of uptime, compatibility, security, error-free operation, or fitness for a particular
purpose. CAS, signatures, journals, and receipts operate within documented assumptions.
They do not guarantee exactly-once external effects or continued native-worker availability.
Offline tests and synthetic benchmarks are not live-deployment or production-performance
assurances. Historical live evidence does not validate the newly added Router, parallel
executor, or optional setup helper. Configured session limits are not endurance guarantees.

Keep credentials private and separate for each endpoint. Obtain the required explicit
approval for new grants, broader scopes, credential storage, and cloud writes.
The `drive.readonly` scope is broader than a dedicated transport folder. Limit sharing
to authorized participants; HMAC is not encryption, and clearing a Doc's current body
does not erase its revision history. Preserve evidence and stop on unknown creation,
write, execution, or delivery outcomes; do not automatically replay them or discard
journals to start over. Do not use the project for critical production or other
high-consequence purposes without an independent assessment of security, privacy,
reliability, and suitability.

This notice explains project boundaries. It is not legal advice, does not replace or
amend the [MIT License](LICENSE) or [third-party notices](THIRD_PARTY_NOTICES.md), and
does not alter any provider's terms. See [SECURITY.md](SECURITY.md) and the linked
validation and live-acceptance documents for the specific limits.
