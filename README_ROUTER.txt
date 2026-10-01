Dots2Codex One-Message Router, repaired merge candidate
====================================================

首次：审阅 docs/ROUTER_ONE_CLICK.zh-CN.md，再运行 INSTALL_ROUTER.command
日常：运行 START_ROUTER.command，把唯一私有 join 消息发给 Dots
状态：ROUTER_STATUS.command
结束：退出 Codex，再运行 STOP_ROUTER.command；检查 authoritative close 和 process_stopped

本包基于已发布 689efa055e6bb2d290f8870e25ff1ebb3a3109bd，保留并强制 Router
使用最新 parallel connector cells。不要覆盖正在运行的 checkout/runtime。
升级、授权前提与完整验收表：docs/ROUTER_UPGRADE.zh-CN.md
Dots 端协议：docs/ROUTER_JOIN_V1.zh-CN.md（消息标记兼容；bootstrap 文档协议已升级 V2）
验证范围：docs/ROUTER_VALIDATION.md

默认 4 小时 / 128 次模型请求，硬上限 8 小时 / 128 次。不是无限会话。
需要已经获授权且活跃的 Dots/native worker；Python 不接纳/唤醒/调用模型。
正式 Control Doc 与 bootstrap Doc 分离；HMAC 证明 join-code 持有，不是平台身份认证。
join-code 不写 Google；不放 shell 参数、日志或发布物。聊天与剪贴板含此私密配对码。
pin/config 会清空当前 bootstrap 正文，但历史版本可能保留；HMAC 不提供加密。
包中不含 OAuth 凭据、真实会话 ID、用户请求或响应。
完整新 Router 的真实 Mac/Google/native 配对与多小时耐久性仍待现场验收。
