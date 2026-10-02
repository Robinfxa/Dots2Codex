# 固定模型与 reasoning effort：每个新会话显式选择

本版本让 Mac 启动要求、真实 native admission 参数、不可变 pin 和每次 Responses 请求
绑定到同一个 `model + reasoning_effort`。**改启动命令里的显示名称并不等于换模型。**
只有可信 Dots 父上下文实际调用平台接纳工具、得到成功返回，并保存精确提交参数与返回身份，
后续配对才能继续。Python 不调用模型，不伪造 admission，也不按请求重新 spawn。

此功能覆盖 `remote_transport` 及 Mac Router 路径；原 POSIX 本地桥接仍使用原来的 legacy 语义。

这是离线实现与测试说明。尚未用真实 Mac、Google 写入和 native 推理完成此新增路径的端到端验收。

## 1. 在 Mac 查看本版本支持的组合

在同版本完整 checkout 中运行，已有 Router 环境可使用 `.venv-router/bin/python3`：

```sh
python3 -m remote_transport.router_mac models
```

此命令只读本地 `remote_transport/native_capabilities.json`，不连接 Google、不做推理。
输出包括 snapshot 版本和 SHA-256、每个模型的 `native_efforts`、`bridge_efforts` 及限制。
这是**版本化能力快照**，不是账号可用性、额度或实时平台模型发现接口。

本版本的模型为 `gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`。
当前 v2 桥接允许的 effort 为 `low`、`medium`、`high`、`xhigh`，新设置默认选中 `xhigh`。`max` 已从可选/可接受的桥接组合中移除；
`native_efforts` 仅记录平台能力观察，不是可选菜单，不能据此解锁 `max`。选择以 `bridge_efforts` 的当前输出为准；
不会把不支持的模型换成默认模型，也不会把未知 effort 归一化成别的级别。

官方 Codex CLI 0.159.2 会改写 `ultra`，因此即使某 native 模型支持它，本桥接仍拒绝该值；
`none`、`minimal`、`persistent` 也不在此桥接快照的可选组合中。不能靠手改 JSON 解锁。
`--catalog /absolute/path/snapshot.json` 只接受与本版本内置快照完全相同的内容，不接受
用户扩展模型列表。快照变化应通过审阅过的代码升级和新会话处理。

旧 v1 设置不会被静默映射到新档位。升级代码之前，先用原匹配版本 Stop/关闭当前 Global 或单会话，并按需 Restore 已应用的 Global 配置；确认旧进程停止后再更新代码。随后打开 `START.command` → `Settings`，核对模型并重新选择 effort，确认保存后才启动新会话。旧 `max`、旧 capability hash、旧签名 JOIN/pin/runtime 都不能原地改成 `xhigh`。保留原有证据，重新生成 JOIN；退出设置或拒绝确认不会改配置。

## 2. 为新 Router 会话指定完整组合

```sh
./START_ROUTER.command --model gpt-6.1-sol --effort xhigh
# --model 可以缩写为 -m
./START_ROUTER.command -m gpt-6-astra --effort high
```

每次只选其中一条命令，不能同时启动两个会话。也可以直接使用模块：

```sh
.venv-router/bin/python3 -m remote_transport.router_mac start \
  --model gpt-6.1-sol --effort xhigh --launch-codex
```

- `--model` 和 `--effort` 必须一起给出；只给一个会失败，不会继承另一个旧值
- 启动在创建 Google 资源、发布 bootstrap 或接纳 native worker **之前**校验组合
- CLI 的完整组合覆盖配置里的下次启动默认值，但不会改写配置，也不会改现有会话
- `configure` 同样接受完整组合，并将经验证的 `model_selection` 保存为未来启动默认值；
  它仍遵循原来的已授权 folder、凭据检查流程，不是只修改模型的离线命令
- 未配置 `model_selection` 且启动不传组合时，维持显式 `legacy_unverified` 模式；
  CLI 仍使用 `native-subagent-bridge`，实际底层模型与 effort 未知，不从名字推断
- 已运行会话不能用重新 `start` 原地切换；先按原规程关闭并核验，再创建新的配对会话

启动后仍按[Router 操作规程](ROUTER_ONE_CLICK.zh-CN.md)将一条私有 join 消息交给已授权 Dots。
消息中的模型提示只是便于操作者查看，权威要求来自已验证签名的 bootstrap `required_selection`。
join code、Google IDs、runtime 和任务内容仍须保密。

## 3. Dots 端必须真实接纳指定组合

已选择组合时，bootstrap 使用 `dots-router-bootstrap/3`；旧的无选择会话保留 V2。
先完成[原配对规程](ROUTER_JOIN_V1.zh-CN.md)中的私有文件、同版本 checkout、`inspect` 和
签名根验证。`inspect` 返回 `required_selection`。该值纳入 HMAC 根，后续不得删除或替换。

为本轮准备只包含 worker 职责与运行边界的私有 `WORKER_MESSAGE.txt`，不要放未来请求正文。
生成一次原生接纳计划：

```sh
python3 -m remote_transport.router_join plan-native \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --task-name router_native_worker --message-file WORKER_MESSAGE.txt \
  --save NATIVE_PLAN.json
```

可信父上下文须将计划中的 `arguments` 原样提交给**真实平台**的 `collaboration.spawn_agent`，
只调用一次。参数包括选择的 `model`、`reasoning_effort` 和 `fork_turns="none"`；
不继承父模型、不复用其他 native identity、不从计划名称自行构造“成功返回”。
若平台没有此工具、拒绝组合、结果不明或返回失败，停止并报告；不静默回退或重新接纳。

将实际调用参数保存为 `ACTUAL_NATIVE_ARGUMENTS.json`，将真实成功返回（包括 `task_name`）
保存为 `ACTUAL_NATIVE_RESULT.json`，并使用同一私有 ledger：

```sh
python3 -m remote_transport.router_join record-native \
  --snapshot BOOTSTRAP_SNAPSHOT.json \
  --document-id BOOTSTRAP_DOCUMENT_ID --tab-id BOOTSTRAP_TAB_ID \
  --join-code-file JOIN_CODE_PRIVATE.txt --state-dir PRIVATE_PAIRING_LEDGER \
  --plan-file NATIVE_PLAN.json --actual-arguments ACTUAL_NATIVE_ARGUMENTS.json \
  --native-result ACTUAL_NATIVE_RESULT.json --save NATIVE_ADMISSION.json
```

随后完成原有双向 raw-file probe。生成 `plan-admit` 时，在原命令中增加
`--admission-receipt NATIVE_ADMISSION.json`，并使用真实返回的 native task identity。
完整路径须沿用同一私有配对 ledger，不换 ledger 绕过一次性 reservation。

Mac 校验接纳回执，创建包含 `inference={selection,admission}` 的全新不可变 deployment pin。
签名链、bundle 校验、正式 Control CAS、worker input、permit/result 与状态报告继续携带或验证
此绑定。每次请求必须精确匹配 `model` 和 `reasoning.effort`。正常 facade/controller 提交
在发布请求、消耗新预算前拒绝缺失 effort 或组合变化。若恶意或篡改的 raw object 绕过前端，
connector 可能已消费一次性 begin/permit；后续校验仍拒绝暴露输入或执行推理，不能回滚消费记录重试。

此回执是 **parent-recorded platform admission evidence**：记录可信父上下文提交的参数和
实际成功返回的任务身份。它不是平台签名 attestation，也不是底层模型遥测。
`underlying_model_verified=false` 必须保留，不能把请求的 model 名称当作已独立验证的实际引擎。

## 4. 官方 Codex CLI 与 `/model`

Router 仍使用未经修改的官方 `codex-cli 0.159.2`，保留 `on-request`、`workspace-write`、
loopback provider、原认证边界与零重试配置，不通过模型元数据覆盖审批或沙箱。

选定模式的启动命令同时设置：

- `--model` 为已接纳组合的模型 slug
- `model_reasoning_effort` 为精确 effort；支持的 reasoning 级别来自本地模型目录
- `model_catalog_json` 为私有 journal 内、只包含当前绑定组合的绝对文件路径

这个单会话目录使用官方 CLI 的本地 ModelsResponse schema；它关闭不受本桥接支持的
`use_responses_lite` 和 effort 更新能力。为 `/model` 提供的本地目录只包含本会话组合，避免提示
用户在一个已绑定会话里切换。实际交互菜单尚待现场验收。全部候选组合请在配对前使用 `router_mac models` 查看。

不设置 `model_supports_reasoning`：官方 0.159.2 不识别此配置键，strict-config 会拒绝它。

**不声称 `/v1/models` 会自动驱动官方 `/model` 菜单。** 也不修改用户全局 Codex 配置或旧 pin。
版本升级需要重新验收目录解析、发送的 Responses 字段以及工具续轮；CLI 自动归一化 effort
不能替代严格的绑定校验。
选定模式同时核对能力快照要求的 CLI 版本，不能仅修改配置的 `expected_codex_version` 绕过。

手动取得现有 facade 的命令：

```sh
python3 -m remote_transport.operator codex-command \
  --ready /absolute/private/runtime/ready.json --workdir /absolute/workspace
```

ready 文件中的选择与本地模型目录会被验证。可再加 `-m gpt-6.1-sol --effort xhigh`
作为**已有绑定的断言**；不同值会报 `session_model_effort_immutable_start_new_session`，
不会修改 worker 或 pin。旧 ready 文件仍生成 legacy 命令，并明确报告底层模型未知。

## 5. 改组合、失败与验证

改组合须结束旧会话、保留旧 journal/history，并使用新 bootstrap、新 native admission、
新 session ID、新 pin 和新 runtime。原有迟到结果只在原绑定下恢复，不搬到新模型上下文。
不通过覆盖 pin、编辑历史、改 receipt 或删除 ledger 来实现“切换”。

常见阻塞：

- `model_and_effort_required_together`：补齐完整组合后创建新会话
- `unsupported_native_model` / `unsupported_model_effort_pair`：查看当前离线 `models` 输出
- `capability_catalog_not_supported_by_this_release`：两端使用相同完整版本；不要自行扩展能力
- `selected_codex_catalog_required` / `codex_catalog_selection_mismatch`：核验当前 ready 与原 runtime，
  不绕过检查或用另一个会话目录替代
- `model_change_requires_new_paired_session` / `effort_change_requires_new_paired_session`：
  请求与 pin 不同；停止当前改动，另开新配对会话
- native 接纳、创建或 CAS 结果未知：保留精确操作记录，只对账，不自动重放

离线回归入口（不做 Google 写入和真实 native 推理）：

```sh
python3 -B -m unittest remote_tests.test_model_selection_launcher -v
python3 -B -m unittest discover -s remote_tests -v
```

新增 launcher 测试覆盖旧配置兼容、完整选择/部分选择拒绝、篡改目录拒绝、精确 CLI 参数、
已有 session 改组合拒绝，以及 bootstrap → native admission receipt → pin 的参数传递。
历史 v1 曾用官方 `codex-cli 0.159.2`、隔离的空 `CODEX_HOME` 和 `debug models` 实际加载
当时全部 25 个组合的绝对路径目录。当前 v2 移除 max 后为 20 个组合；旧记录不当作本轮新现场验收。
此检查不做推理，不证明交互 `/model` 菜单或线上 native 路由；完整端到端仍待授权验收。

实现依据：官方 Codex
[0.159.2 ModelsResponse schema](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/protocol/src/openai_models.rs)、
[effort 归一化](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/protocol/src/openai_models/reasoning_effort.rs)。
平台模型列表来自本版本附带的能力快照，不把公开 API 模型清单与 native 平台接纳权限混为一谈。
