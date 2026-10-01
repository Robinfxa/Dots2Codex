# Mac Router 操作手册

依据：同包修复版的 shell 入口、router_mac.py、operator.py。以下是该包命令，不是推测的 CLI。配置/运行数据一律在私有目录保存。[R1,R2,R4]

## A  已有环境 最短路径

保留旧的 `~/Dots2Codex` 与 long-session 测试目录。把新包解压到一个新的目录；使用这个包根目录里的四个 `.command`。不需要重新建 Cloud 项目，不需要手动造 pin/config，不用手动建 Control Doc。

首次双击 `INSTALL_ROUTER.command`：填原已批准的 folder ID、现有 `authorized-user-bidir.json` 路径、独立 workspace。已有目标 `router.json` 时配置写入拒绝静默覆盖。先运行旧环境 STATUS/STOP，核验正式控制关闭并保留证据，再按 `docs/ROUTER_UPGRADE.zh-CN.md` 审核配置迁移。不要删活动指针或 journal 掩盖旧会话。

日常双击 `START_ROUTER.command`，将自动复制的一段 join 消息只发给匹配版本的 Dots。消息会在本机终端出现并复制到剪贴板；不发给 Gemini。发送后清空剪贴板。等真正 `ROUTER_READY` 并自动打开 Codex。

## B  原生 Terminal 的等价操作

先 `cd` 到包含以下文件的包根目录。仅在下载来源和哈希已核对后运行：

```sh
bash INSTALL_ROUTER.command
bash START_ROUTER.command
```

安装器本身不会发 OAuth consent；如果尚无 authorized-user 文件，先按主手册第 6 节完成授权。出现包依赖下载失败，应检查网络和固定 requirements，不要随意将所有版本改成 latest。

`START` 会保持 Terminal 的交互输入。不要通过 `python3 - <<'PY'`、管道或无 TTY 的后台进程接管 Codex；它需要真实终端。

## C  配置实际默认值

| 配置键 | 默认 | 意义 |
|---|---:|---|
| seconds | 14400 | 默认四小时；硬上限 28800 秒，不保证 worker 存活 |
| max_requests | 128 | 模型请求预算，含工具往返 |
| scope | responses_tools | 可返回已公布 schema 下的工具意图 |
| port | 0 | OS 分配端口，不固定 8765 |
| deadline | 1800 | 单次 HTTP 等待预算 |
| poll_interval | 5 | facade 轮询配置 |
| heartbeat_interval | 15 | SSE 心跳配置，不是新模型回答 |
| bootstrap_ttl | 1800 | join 等待期 |
| expected_codex_version | codex-cli 0.159.2 | 本包启动的精确版本门槛 |

普通配置：`~/.config/dots2codex/router.json`。
活动指针：`~/.config/dots2codex/router-active.json`。
证据目录：`~/.config/dots2codex/router-sessions/router-.../`。

配置文件存凭据路径；`join-message.txt`、`private-pairing.json` 含配对秘密，不能当普通日志上传。正式消息正文可能包含用户任务和代码上下文，应按敏感运行资料管理。

## D  状态检查 不要用历史 runtime

双击 `ROUTER_STATUS.command`。或者在包根目录新开 Terminal：

```sh
. .venv-router/bin/activate
python3 -m remote_transport.router_mac status
```

重点看：`stage`、`bootstrap_stage`、`bootstrap_bundle_present`、`closed`、`process_stopped`、`bootstrap_cleanup`、`worker_stop_confirmed`、`facade_alive` 与可取得的 bridge 状态。不要把状态输出完整公开，因为它包含私有资源 ID、runtime 路径。

需要 operator 时，从当前活动指针取路径，不复制旧测试的 `session-...`：

```sh
export RUNTIME="$(python3 -c 'import json,pathlib; p=pathlib.Path.home()/".config/dots2codex/router-active.json"; print(json.loads(p.read_text())["runtime"])')"
python3 -m remote_transport.operator status --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller"
```

如果活动文件不存在、字段缺失、ready 文件缺失，先停止并检查当前阶段；不自行创建空文件补齐。

## E  迟到结果 不重新发 prompt

从真实 `operator status` 中取原 request ID。以下 ID 是占位符，不要原样运行：

```sh
export REQUEST_ID='ACTUAL_64_HEX_REQUEST_ID'
python3 -m remote_transport.operator result --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller" --request-id "$REQUEST_ID"
```

这会读原请求的结果，不是重新推理。结果已在 Mac 实际看见并核对后，取真实 result ID：

```sh
export RESULT_ID='ACTUAL_64_HEX_RESULT_ID'
python3 -m remote_transport.operator ack --ready "$RUNTIME/ready.json" --journal "$RUNTIME/controller" --request-id "$REQUEST_ID" --result-id "$RESULT_ID" --evidence 'Observed and verified the actual result on this Mac'
```

只有真正观察后才能写上述 evidence。不要用下载成功、socket flush 或 Dots 的“我已经写了”代替 Mac 观察。不要 ACK 工具执行状态不明的回合。HTTP 返回 200 也仍需核对响应体。

## F  正常关闭

先停止发新任务，并核对所有请求是否 confirmed。退出 Codex，然后运行：

```sh
bash STOP_ROUTER.command
```

STOP 是本包的关闭入口：先持久化 stop 标记，再等待启动临界区完成，直接访问正式 Control Doc 提交或对账 close。即使 facade 已退出或 ready 文件不存在，也不能跳过权威关闭。不要把 operator 的在线 facade close 当作 STOP 的替代品。

- `closed=true`：正式控制记录已核验关闭，阻止未来 admission、claim 和 begin
- `process_stopped=true`：本地已知会话进程已停止；必须验证 PID 与 OS 身份，不按同名进程批量 kill
- `bootstrap_cleanup`：独立说明当前正文清理成功、失败或未知，不证明历史擦除
- `worker_stop_confirmed=false`：没有平台停止回执，Dots 仍需读 close、对账已知结果并合作停止

权威不可读、缺失或写入结果不明时使用 `RECOVERY_REQUIRED`，保留 runtime 与操作证据。未知 close 只读对账精确 operation ID，不重发 CAS；只有明确 CAS 拒绝才可在下一次显式 stop 重新规划。

close 不撤销已消费的 begin，也不自动撤回已执行的命令或强杀平台任务。停止正在启动的会话也必须等待其临界区，不以本机进程消失推断 Google 控制记录关闭。

## G  会话结束后保留什么

保留源码包、配对与执行记录、pin/config、Control Doc、已下载协议对象和测试文件。默认不要复制活跃 journal 到另一端，也不要用旧备份回滚继续运行。新会话只能通过新一轮启动创建，不能手工把 `closed` 改回 false。

TLS、403/404、TTY、thread limit 等具体排错见 `05_TROUBLESHOOTING.zh-CN.md`。
