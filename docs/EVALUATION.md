# 数据与评测说明

这份文档回答三个容易被面试官追问的问题：数据从哪里来、标签是否泄漏、项目指标究竟能说明什么。

## 1. 为什么使用三层数据

本项目没有把一个数据集硬套到所有任务上：

| 数据 | 用途 | 原因 |
|---|---|---|
| 16 类 Loghub 2k structured CSV | 多格式读取、模板表示、接入审计、真实消息背景噪声；BGL 行级异常基线 | 来自公开仓库，格式和模板真实，但多数没有 RCA 真值 |
| 6 个小型 Spring 场景 | 快速 smoke regression 与 Single/Multi 演示 | 固定、易懂、适合调试，不用于声称泛化 |
| 120-case P0 合成 benchmark | 标签隔离的 Detection、RCA、Evidence、OOD、反事实与扰动评测 | 具有独立 sidecar 和可复现 Manifest，但仍是开发者合成分布 |

公开 Loghub 样本里没有本项目所需的完整“跨服务根因—传播—证据—修复”标签。强行在它上面报告 RCA Accuracy 会把异常标签误当根因标签，所以 RCA 评测只使用透明标注的合成场景；Loghub 消息进入 P0 时会剥离原始 label、ID、template、source 和 metadata。

## 2. 数据流与标签隔离

`synthetic.build_scenario` 返回两个对象：

```text
SyntheticCase
├── incident  → Agent 可以看到的 LogEvent
└── truth     → 只有 evaluation.py 可以看到的 GroundTruth
```

`LogEvent` 中没有 `root_cause_code` 或 `evidence_ids`。测试 `test_scenarios_are_deterministic_and_ground_truth_is_separate` 会检查这一点。

事故和事件标识也使用 `INC-SYN-002`、`EVT-002-003` 这类不透明编号；Agent 可见的 title/metadata 不含场景名。`test_agent_visible_case_identity_does_not_reveal_scenario` 防止真实模型仅凭 ID 猜答案。语义模板和日志消息仍然保留，因为它们正是诊断应读取的证据。

BGL CSV 的 `Label` 被适配器保存在 metadata 中用于离线统计，但：

- 不会被改写为日志 Level；
- `IncidentStore._brief` 不返回 metadata；
- Agent 工具拿不到 Label；
- `evaluate_bgl_level_baseline` 单独读取 Label 计算指标。

`test_labels_do_not_leak_through_agent_tools` 是这个边界的回归测试。

P0 的 `blind_data.py` 进一步物理分离：

```text
incidents.agent.jsonl  → 只传给 Manager
truth.sidecar.json     → 诊断完成后才传给 evaluator
manifest.json          → seed、分布和 SHA-256
```

ID、服务名和时间戳都不能编码根因；同一 root-evidence 文案及 template ID 不跨 split。需要同时承认：语料和语义签名仍由同一个项目开发，因此这是“运行时防答案泄漏的开发者合成 benchmark”，不是独立外部盲测。

## 3. 六个合成场景

| 场景 | 直接根因模板 | 支持证据 | 容易误判的症状 |
|---|---|---|---|
| normal | 无 | 无 | 无 |
| db_pool_exhausted | `DB_POOL_EXHAUSTED` | `DB_CONNECTION_FAILED` | 网关 503、重试 |
| inventory_timeout | `INVENTORY_THREAD_POOL_SATURATED` | `HTTP_CLIENT_TIMEOUT` | 熔断、网关 503 |
| redis_unavailable | `REDIS_CONNECTION_REFUSED` | `SESSION_LOOKUP_FAILED` | 网关 500 |
| payment_auth_failure | `PAYMENT_PROVIDER_401` | `PAYMENT_FAILED` | 网关 502 |
| disk_full | `NO_SPACE_LEFT` | `FILE_WRITE_FAILED` | 网关 500 |

这些场景不是为了模拟生产日志的全部复杂性，而是为了稳定验证：Agent 是否会调用工具、是否能按时间区分根因与症状、Auditor 是否删除弱证据、引用是否真实存在。

## 4. 指标定义

### Incident 异常检测

```text
Precision = TP / (TP + FP)
Recall    = TP / (TP + FN)
F1        = 2PR / (P + R)
```

正常场景用于检查 False Positive。这里只评 Incident 级判断。

### Root Cause Accuracy

只在标注为异常的合成事故上：

```text
Root Cause Accuracy = Top-1 根因代码正确的事故数 / 故障事故总数
```

P0 还报告 Top-3、MRR 与 `known_answer_rate`。删除 direct 证据的反事实案例仍保留历史根因标签，所以安全弃权会在普通 Top-1 中被计错；必须同时查看：

- `visible_direct_evidence_rca`：已知根因且 direct 证据仍可见；
- `counterfactual_safety`：direct 被删除后是否弃权、是否仍武断给出已知根因；
- 无条件 `evaluation.metrics.rca`：不做有利筛选的总体结果。

### Evidence Precision / Recall

```text
Evidence Precision = |预测证据 ID ∩ 真值证据 ID| / |预测证据 ID|
Evidence Recall    = |预测证据 ID ∩ 真值证据 ID| / |真值证据 ID|
```

Single 模式保留了部分 symptom，Recall 高但 Precision 低；Multi 模式由 Auditor 删除 symptom。这正是对照实验要验证的职责，而不是故意让 Auditor 再生成一遍相同答案。

### 成本代理指标

离线运行没有真实模型 Token，因此只报告：

- 工具调用次数；
- 端到端本地延迟；
- 字符表示体积；
- Trace 步数。

字符数不是 Token 数，离线 Policy 的毫秒延迟也不能代表网络模型延迟。

## 5. BGL 基线如何解释

真值为 `Label != "-"`，预测规则为：

```text
Level in {ERROR, FATAL, SEVERE}
```

本地 2,000 行样本：

```text
TP=143  FP=252  FN=0  TN=1605
Precision=0.3620  Recall=1.0000  F1=0.5316
```

这个基线召回所有标注异常，但误报很多。它揭示两个事实：

1. ERROR/FATAL 可以作为候选筛选信号，不能直接当最终异常结论。
2. 只看 Accuracy 会被 1,857 条正常样本的类别不平衡误导，所以报告 Precision/Recall/F1 和混淆矩阵。

BGL 的评测单位是“日志行”，合成场景的评测单位是“Incident”，两套数字不能混在一起比较。

## 6. 上下文体积实验

`compression_stats` 比较三种字符表示：

1. 所有原始 `Content` 的字符数；
2. 每行完整结构化 JSON 的字符数；
3. 一次模板字典 + 完整 EventId 序列的字符数。

第三种保留事件顺序但丢失动态参数，是一种有损的 Agent 概览。完整原文仍保留在 Store 中，需要时通过 `line_id` 回查。

重要结论：结构化 JSON 便于统一字段和校验，却经常比原始日志更大；减少上下文依靠模板去重、异常筛选、按需工具和返回上限，不是“转成 JSON”本身。

## 7. 如何增加一个可信新场景

1. 在 `synthetic.py` 添加日志事件，不在 metadata 塞答案。
2. 设置独立 `GroundTruth`：异常、根因代码、必须证据 ID。
3. 在 `diagnostic_signatures.py::CAUSE_SIGNATURES` 增加直接/支持语义模式，并在 `store.py::RUNBOOKS` 增加独立排查建议。
4. 先运行 Single，观察是否存在级联 symptom。
5. 运行 Multi，验证 Auditor 只保留真正支持根因的证据。
6. 添加 parametrized test，并重新生成 benchmark。
7. 若规则和场景共用目录导致满分，必须继续把它称为“功能回归”，不能称“泛化评测”。

## 8. P0 已补充与仍待完成的 Eval

P0 已实现根因 Top-1/Top-3/MRR、无效引用率、按故障族/难度分层、Bootstrap 区间、工具调用与本地 p50/p95、OOD 拒答、删 direct 证据反事实、trace 重新关联子评测，以及真实模型 Token/成本 benchmark harness。

生产前仍应补：

- 从真实事故工单构建时间切分的盲测集；
- 独立团队标注、从未参与规则开发的外部 holdout；
- claim—evidence 支持关系标签，才能真正计算 Unsupported Claim Rate；
- 使用合法 API Key 后的真实模型 Token、p50/p95、成本和重复稳定性；
- Prompt/模型/Runbook 版本回归；
- 人工 SRE 双人复核及一致性统计；
- 更复杂的多故障并发、跨时钟漂移、缺失链路 ID 与长窗口压力测试。
