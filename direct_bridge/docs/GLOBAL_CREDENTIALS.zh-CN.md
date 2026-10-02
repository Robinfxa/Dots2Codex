# 全局一键启动：本机凭据契约

这份说明适用于新的全局启动器和 `global_credentials.py`，不改变旧的单路由试验协议。
构建与自动测试只使用合成值；没有读取、生成或存储真实账号凭据。

## 首次运行与以后双击

启动器先复用已有的授权凭据，按以下顺序选择每个值；读取已授权的现存文件不会额外要求 REUSE 确认：

1. 启动进程中已有的 `CONTROL_PLANE_API_KEY` / `DOTS_BRIDGE_HTTP_BEARER`
2. 所选私有凭据目录下的 `private.env`
3. 仅对本地 HTTP bearer，复用同一目录下已有的 `http-bearer`

默认凭据目录是 `~/Library/Application Support/Dots2Codex Direct`。启动器把这个独立的 `credential_dir` 记录在非秘密设置中；全局网关运行状态可以使用另一个 `Dots2Codex Direct Global` 目录，不会移动或重置旧 route 数据库。后续 supervisor 从同一个已确认的凭据目录读取。
不会搜索整个主目录、钥匙串、其他项目的 profile 或 Lean 凭据。
如果明确提供的环境变量格式无效，则停止，不会悄悄退回另一个值。
两个凭据必须不同。

仅缺失的值会在本机 Terminal 中通过隐藏输入询问。请使用已经授权的对应凭据；此入口不会创建 API key，也不会把输入发给聊天助手。隐藏输入不可用或输入不是交互式 Terminal 时，停止而不是降级为回显输入。

如果以后启动还需要保存某个值，会另外列出要保存的变量名和完整的 `private.env` 路径，请用户输入 `SAVE`。默认回车拒绝保存；当前这一次仍可在进程内使用这些值。更新已有文件同样需要新的本机确认，不展示含凭据的差异。已复用的 `http-bearer` 不会被迁移、覆写或重复保存。

首次运行完成后，启动器和后台监督进程可以直接加载已确认的私有文件。Finder 无需继承之前 Terminal 的环境变量，也无需打开两个终端来回复制凭据。如果拒绝了保存，仅当该次启动的父进程把内存中的凭据通过进程环境传给监督进程时，此次运行仍可继续；下次独立启动可能再次请求隐藏输入。

## 保存格式与文件保护

`private.env` 是数据文件，禁止用 `source`、`eval` 或 shell 执行。写入格式为：

```text
# Dots2Codex private data. Do not source this file in a shell.
CONTROL_PLANE_API_KEY="此处仅为说明，不能作为真实值"
```

上面的占位内容不是可用凭据，不应复制运行。实际写入使用 JSON 字符串编码。解析器只接受两个已知的变量名以及单行可见 ASCII 值；不允许 `export`、重复字段或其他键名。美元符号、反引号和命令替换文本都只作为原始字符处理，绝不会执行或插值。

- 私有状态目录必须归当前用户所有，权限为 `0700`
- 凭据文件必须归当前用户所有，权限为 `0600`，且是没有额外硬链接的普通文件
- 拒绝符号链接目录、符号链接文件、其他用户所有的目录、不安全的可写父目录、过大文件和仓库内路径
- 只在同一私有目录创建临时文件，刷新后原子提交；新文件提交不覆盖竞争创建的目标
- 对已有文件，在确认前后检查内容和文件身份；文件发生变化时拒绝覆盖
- 此工具不会自行放宽权限，也不会修复来源不明的文件；请用户在本机核实后处理

## 不同进程能拿到什么

| 进程 / 保存位置 | 本地 HTTP bearer | Tunnel 控制面 key |
| --- | --- | --- |
| 官方 tunnel-client | 有，传给其专属 MCP 子进程 | 有，仅用于隧道控制面认证 |
| MCP Python / loopback bridge | 有 | 无；shell 包装器必须在启动 Python 前移除 |
| Codex 客户端 | 有，按受支持的本地 provider 配置使用 | 无 |
| 经用户确认的私有 Codex provider 配置 | 可以有仅限 loopback 的 Authorization header | 严禁 |
| 路由 JSON、命令行参数、状态预览、日志、错误信息 | 无 | 无 |

启动器的子进程环境使用最小系统变量集合；不继承其他模型 API key 或 `PYTHONPATH` / `BASH_ENV` 等启动钩子。凭据对象的文本表示隐藏值。任何调用方都不得输出其字段或进程环境。

桌面 Codex 不应依赖 Finder 是否继承 Terminal 环境。需要保存 loopback bearer 到私有 Codex provider 配置时，配置事务必须另外明确说明目标路径和用途并取得本机确认。该配置及包含其内容的备份必须保持私有权限。Tunnel key 不得写入 provider、profile 明文、路由配置或配置差异。

## 停止、恢复和清除

正常停止、恢复 Codex 原配置不会删除 `private.env` 或 `http-bearer`，避免下一次启动重新索取凭据。

用户明确想删除本机保存副本时，可从源码根目录在本机运行：

```sh
python3 -m direct_bridge.global_credentials clear
```

它会再次列出完整路径并要求输入 `CLEAR`。默认只删除 `private.env`；如果也要删除专用的 `http-bearer`，需明确加上 `--include-http-bearer`。自定义状态目录使用 `--state-dir`。不要把凭据值放到命令行参数。

清除本地副本并不在服务端撤销 API key，也不会清空运行中进程的内存或已确认的 Codex provider 配置。需要断开使用时，先停止 bridge 并恢复配置；需要撤销或轮换服务端 key 时，由用户通过对应服务的官方界面操作。此工具不会自动执行这些安全敏感动作。

## 供启动器使用的 Python API

- `load_credentials(state_dir, environ=None)`：只读，无提示、无文件修改；缺失时抛出不含值的 `CredentialError`
- `prepare_credentials(state_dir, environ=None, interactive=True)`：先复用，缺失才隐藏输入，保存前单独确认
- 返回对象的 `local_bearer` / `http_bearer` 用于 loopback / 已批准的私有客户端配置
- `tunnel_env()`、`bridge_env()`、`codex_env()` 返回不同的最小进程环境；不得把返回值记录到日志或 JSON
- 单独的 supervisor 必须通过进程环境接收本次 session-only 值，再调用只读加载器；不能通过参数或临时非私有文件传递
- `forget_saved_credentials(..., include_http_bearer=False)` 仅供明确的清除动作调用，不得接在 stop、restore、崩溃清理或测试收尾后

运行合成测试：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s direct_bridge/tests -p test_global_credentials.py -v
```
