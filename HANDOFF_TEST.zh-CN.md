# 另一位worker只按文档完成的确定性交接测试

这是明确标记的合成数据测试，不启动Codex、不监听HTTP、不调用真实原生推理。不要把它的结果写成原生端到端通过。只需要Python3.11+和此包；两位操作方可各用自己看到的同一共享目录绝对路径。

## 准备方

选择一个不存在的新目录BRIDGE_ROOT，OWNER例如handoff-owner：

```sh
python3 handoff_fixture.py --root "$BRIDGE_ROOT" prepare --owner "$OWNER"
```

这一个专用fixture创建部署、同环境探针记录、合成ready元数据和一条随机nonce请求。它不是生产启动方式。将本包位置、BRIDGE_ROOT、OWNER和本文交给另一位已有worker；不要传其他历史或源码知识。测试窗口300秒。

## 接手方

本测试的接手方是已有的活跃原生worker，因此可以如实声明自身具备原生能力；但本测试不用该能力生成结果，而执行确定性fixture。

```sh
python3 portable.py --root "$BRIDGE_ROOT" assign --owner "$OWNER" --role broker --worker handoff-worker --native-capability-attested --save "$BRIDGE_ROOT/evidence/assignment.json"
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$BRIDGE_ROOT/evidence/assignment.json" claim --wait 20 --lease 180 --save "$BRIDGE_ROOT/evidence/ticket.json"
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$BRIDGE_ROOT/evidence/assignment.json" read --ticket "$BRIDGE_ROOT/evidence/ticket.json" > "$BRIDGE_ROOT/evidence/synthetic-input.json"
python3 handoff_fixture.py --root "$BRIDGE_ROOT" reply --input "$BRIDGE_ROOT/evidence/synthetic-input.json" --result "$BRIDGE_ROOT/evidence/synthetic-result.json"
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$BRIDGE_ROOT/evidence/assignment.json" complete --ticket "$BRIDGE_ROOT/evidence/ticket.json" --result "$BRIDGE_ROOT/evidence/synthetic-result.json"
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$BRIDGE_ROOT/evidence/assignment.json" status --ticket "$BRIDGE_ROOT/evidence/ticket.json"
python3 broker_bootstrap.py --root "$BRIDGE_ROOT" --assignment "$BRIDGE_ROOT/evidence/assignment.json" stop --code normal_exit
```

向准备方报告：每步退出码、任务ID、complete是否接受、status是否completed/waiting、有无需要猜测的步骤。不要公开ticket或原始输入。

## 准备方验收

```sh
python3 handoff_fixture.py --root "$BRIDGE_ROOT" verify
```

预期passed=true、state=completed、delivery=waiting。waiting在本测试中是正确结果：没有HTTP消费者，不能伪造delivered。证据位于evidence/handoff-verified.json。该fixture不代表真实跨环境、真实推理、用户Mac部署或无人值守调度已经验证。
