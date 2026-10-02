> **已归档：下文仅供旧单路 CLI 试验参考，不是当前全局版的操作指南。**
> 当前请从 [Direct 全局版指南](../GLOBAL_DIRECT_README.md) 开始，使用根目录 `DIRECT.command` 的 Start / Stop / Restore；要求 Python 3.11+。
> 下文旧的试验目录、试验专用 CLI 启动参数、`REOPEN`、双 Terminal 环境变量及“不改全局配置/不保存凭据”等描述，只属于保留的旧脚本。不要用于全局版。
> 原生控制端请使用 [当前全局参数和真实子任务流程](docs/GLOBAL_NATIVE_CONTROLLER.zh-CN.md)；MCP 名称相同不代表旧 schema 可直接复用。

# Dots2Codex Direct：Mac 从这里开始

新分支的入口是仓库根目录 **`DIRECT.command`**。按 [Mac 快速指南](docs/MAC_QUICKSTART.zh-CN.md) 做完这六步：

1. 在原来的 `main` 先运行 `./LIGHTWEIGHT.command stop` 和 `./LIGHTWEIGHT.command restore`，并结束当前 v3 的旧原生任务
2. 将 `feat/direct-mcp-trial` 放进新的独立 worktree，保留原目录和未提交工作
3. 运行 `DIRECT.command setup`，按提示确认专用依赖环境和新的有界 route
4. 用已授权的官方隧道和 runtime key 建立专用 stdio profile；由你完成 workspace / 插件连接
5. 确认实际 dot 和同一个原生子会话都能调用八个 bridge 工具，再在第二个终端运行 `DIRECT.command codex`
6. 先完成只读三轮闭环验收，结束时退出 Codex、结束子会话并停止前台隧道

首次登录、凭据输入和持续访问授权仍需你完成。一键入口不会生成/保存 key、创建隧道 profile、改全局 Codex 配置或自动把工具装进 dot。执行依赖环境变量的步骤请用 Terminal；Finder 启动不一定继承它们。完整命令和每步检查都在快速指南里。

## 已验证与未验证

已在云端 Linux 使用真实 loopback HTTP、真实 MCP stdio 管道和官方 Python MCP SDK 验证合成客户端的连续回调、单一逻辑 actor、完整 bootstrap 后增量、schema 精确查询、防重复、取消和保守恢复。测试不含真实用户会话或凭据。

固定 Codex CLI `0.159.2` 的参数/源码已核对；云端实际 CLI 请求试验在其自身 sandbox 初始化检查处被阻止，未到 HTTP，因此没有把它算作端到端通过。

**真实 Mac、Secure MCP Tunnel、原生子会话工具可见性、实际模型接纳和 Mac 工具闭环仍未验收。** 合成毫秒级延迟不代表真实速度；逻辑 ID、token 或 hash 也不能证明平台身份、模型已阅读或没有压缩上下文。

## 开发者离线检查

在 `direct_bridge` 目录，用已准备好依赖的 Python 3.10+：

```sh
python3 -B scripts/verify_release.py
python3 -B scripts/run_focused_tests.py
./scripts/start_mcp.sh --help
```

`verify_release.py` 验证 direct 子包清单；仓库新增入口不属于旧版清单，不能拿旧入口绕过其校验。测试不会执行 Mac 命令、连接模型或建立隧道。

详见 [隧道说明](docs/MCP_AND_TUNNEL.md)、[最短原生控制器](docs/NATIVE_CONTROLLER.zh-CN.md)、[CLI 配置与桌面边界](docs/CODEX_CLIENT.zh-CN.md) 和 [真实验收](orchestration/README.md)。

超时、断线或返回丢失都可能表示结果未知。重读既有 request/action 状态，核对真实结果；不要换 ID、删数据库或换 worker 重放副作用。取消不表示动作已回滚。

源码与许可见 README.md、LICENSE、THIRD_PARTY_NOTICES.md。包内清单只证明内容一致性，不代替可信来源核对或现场验收。
