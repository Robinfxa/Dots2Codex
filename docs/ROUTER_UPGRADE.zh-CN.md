# Router 候选升级与真实验收清单

## 来源与范围

- 合并基线：已发布 `689efa055e6bb2d290f8870e25ff1ebb3a3109bd`
- 旧 Router 输入：`Dots2Codex-OneClick-Router-v1-20260930.zip`
- 输入原始 SHA-256：`42d5008e1287fa3ee30e822a1e5eb4787d0465c857c8fb7100c879ea6cc143e6`
- 不是原包替换：保留基线全部 public paths，包含旧 Router 缺失的 15 个 parallel/files/timing 文件
- 本候选修改本地隔离源码并执行离线回归。未安装到 Mac、未创建真实 Google 资源、未配对原生
  worker、未更新正在运行的部署、未推送 GitHub

## 升级顺序

1. 核对交付 ZIP 的 SHA-256，以及 `ROUTER_PACKAGE_MANIFEST.json` 中每个公开文件的 size/hash。
   manifest 的 source tree digest 定义为其列出的路径→SHA-256 字典按排序紧凑 JSON 编码后的 SHA-256；
   manifest 自身不参与该字典，避免自引用
2. 等待原会话完成。确认结果交付/回执与 authoritative close，保留原源码和私有 runtime
3. 在新的独立目录解压候选；不要覆盖正在运行的 checkout、journal、pin 或 worker
4. Mac 按安装指南使用自己已有且已批准的凭据文件；Dots 端使用相同候选 release。不得把 OAuth
   文件或 join-code 放进公开包、源代码、命令参数、版本库或不必要的日志
5. 确保 Google 写入、指定 folder、bootstrap/Control 创建与真实原生 worker 运行都在用户批准范围；
   缺少授权时停下取得明确批准。不要把离线测试许可当作真实云写入/安装许可
6. 开始一个新 Router 会话。V1 bootstrap/join 账本不迁移、不克隆；新会话产生全新身份、pin、nonce、
   0700 runtime 和单次操作记录

## 真实验收（均待执行）

在明确批准的临时 workspace/folder 中，一次性核验下列项目；不使用生产任务正文做探针：

- [ ] Mac 官方版本/参数检查，配置 scope 与 folder 可读
- [ ] forward probe：Mac 创建的确切原始文件 ID，可由 connector 下载并核验 metadata/hash/nonce
- [ ] reverse probe：connector 返回的确切文件 ID，可由 Mac OAuth 读取并核验 parent/name/raw bytes
- [ ] 只发送一条 join 消息；根上下文 HMAC 验证成功，真实平台身份与 pin 相同
- [ ] 正式 Control Doc 与 bootstrap Doc 不同；同一完整 get 提供 tab/revision/state
- [ ] 快速推进：worker CAS 后 Mac 已进入下一阶段，worker 仍以精确签名 operation 历史对账成功
- [ ] Router READY 前 worker 确实运行并进入规定的 parallel cell 长循环
- [ ] 同一 Codex thread 两次文本请求，第二次严格依赖上次实际结果；交付回执与下一次 admission 串联
- [ ] 一次获批准的 workspace 工具调用与后续 tool-result 模型轮；保持 Mac 审批与 sandbox
- [ ] parallel 三对象上传、exact-ID metadata/raw 验证、全验证屏障及 result CAS；记录实际阶段时间
- [ ] stop during pairing / stop during facade startup / readiness timeout：不重启已停止会话、不遗留活服务
- [ ] facade 已死或 ready 缺失时，stop 仍核验 authoritative close；无法核验时明确 RECOVERY_REQUIRED
- [ ] lost CAS / upload response：精确 ID 对账，没有第二次推理、第二次 begin 或盲写入
- [ ] 正式 close 阻止未来 admission；已消费的执行按已知结果规则处理，取得 worker 的合作停止证据
- [ ] 区分当前正文清空与历史保留；按现有授权保留/清理证据，不擅自删历史或扩大分享
- [ ] 在实际平台支持条件下进行 4 小时/128 次耐久性与上下文体积验收；8 小时是协议上限，不是平台保证

## 停止条件与诚实报告

权限拒绝、需要新增授权、身份不匹配、源文件变动、过期/回滚、坏 HMAC、未知 begin 或缺少原始
字节证据，都应停止依赖流程。不要改用另一环境绕过拒绝。未知外部写入可留下孤立资源：读取私有
operation ID/返回 ID 后人工对账，不能假称未创建。停止本地进程不等于撤销已消费的原生执行。

离线通过仅说明合成端口/本地执行路径满足测试，不证明 Google 实际工具形状、Mac OS PID/启动行为、
网络权限、端到端延迟改善或平台任务持续性。最新测试实录见 [ROUTER_VALIDATION.md](ROUTER_VALIDATION.md)。


## 模型选择版本迁移

显式选择模型/effort 的新会话使用 bootstrap V3 和带 `inference` 的部署 pin；
正式 Control binding 也带 `selection`。普通对象 envelope 仍为 `dots-drive-objects/0`，
其中部署 payload 的带版本 `inference` 扩展是新代码必需识别的字段。旧代码会拒绝此 pin，
不能混用旧 worker 或伪装成 legacy。两个端点须使用同一完整新版本。

无选择的旧 pin/V2 可以读取并按原来 `native-subagent-bridge` 语义运行，
不会推断它实际运行哪个模型或 effort。不要把新字段追加到已有 pin、CAS、journal 或 ledger。
升级、选不同模型和选不同 effort 均使用全新 session ID、runtime、native admission 和 pin；
旧历史、未知工具结果和迟到响应保留在原会话，不自动迁移或重放。

通用 CAS `rebind` 明确禁止从或到 selected binding，哪怕会话当前 IDLE/DELIVERED；
本版本没有实现同会话热切换。新 checkout 的 worker 源 hash 列表还覆盖选择验证器和能力快照，
正在运行的旧 materialization 不可原地覆盖。


## 六文件 source-hash 消费端修复

若配对成功后生成首个 cell 报 `router_parallel_runtime_source_changed`，先核对是否为
[已知六项/四项合约不一致](ROUTER_SOURCE_HASH_FIX.zh-CN.md)。修复保留全部六项校验，
但会改变已固定的 `connector_cell.py` digest，因此必须新会话；不要改旧 materialization
记录或为继续运行而删掉 `selection.py` / `native_capabilities.json` 两项。
