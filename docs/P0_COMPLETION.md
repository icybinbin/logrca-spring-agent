# P0 完成说明：离线可复现的日志 RCA Agent Demo

> 本文的测试数量和评测数字是 P0 阶段的历史快照。当前 DeepSeek、文件上传和证据核对入口见 [README](../README.md) 与 [QUICKSTART](../QUICKSTART.md)；最新版测试结果以当前提交的 CI 或本地运行记录为准。P0 离线结果不能作为真实模型的效果指标。

> 本文是当前 P0 的源码学习入口，也是面试时区分“已经实现”“离线测过”“真实模型待测”“生产设想”的事实清单。
>
> P0 的目标不是证明系统已经能处理任意生产故障，而是把日志接入、事故聚合、Agent 工具调用、证据校验、盲测评估和可选真实模型基准串成一条可运行、可复现、可审计的工程链路。

## 1. 先给结论：P0 做到了什么

P0 已经具备以下闭环：

```text
Spring/Logback 文件或 Loghub structured CSV
                    │
                    ▼
           解析、标准化为 LogEvent
                    │
                    ▼
        trace/request/block ID + 时间窗关联
                    │
                    ▼
                 Incident
                    │
                    ▼
           确定性异常门控 Anomaly Gate
              │正常                 │异常
              ▼                     ▼
        不调用诊断 Agent       Incident Manager
                                    │
                     ┌──────────────┴──────────────┐
                     ▼                             ▼
             Hypothesis Agent              Evidence Auditor
              生成根因候选                  multi 模式独立审证
                     └──────────────┬──────────────┘
                                    ▼
                         代码级 Grounding Guardrail
                                    │
                                    ▼
                    DiagnosisReport + Trace + Eval
```

同时，P0 增加了一条与应用链路隔离的评测链路：

```text
Label-separated dataset generator
        │
        ├── incidents.agent.jsonl ──► Agent/Manager ──► Predictions
        │
        └── truth.sidecar.json ───────────────────────► Evaluator
                                                        │
                                                        ▼
                         detection / RCA / evidence / grounding / efficiency
```

这里最重要的事实是：`IncidentManager` 只收到 Agent 可见的 `Incident`，真值在诊断全部结束后才由评测层关联。测试也会检查 Agent 输入中不存在 `root_cause_code`、`evidence_ids`、`fault_family` 等真值字段。

### 1.1 当前验收快照

- P0 数据版本：`blind-rca-v1.3`；seed：`20260921`；Manifest SHA-256：`9f6f1f4267722e2725916300e5c0e38b7c38d930081bb0ac6cda19d472adf464`。
- 40-case test：Detection F1 `1.0000`，无条件 RCA Top-1 `0.8125`，direct 证据可见子集 Top-1 `1.0000`，OOD 和删除 direct 证据后的弃权率均为 `1.0000`。
- Multi 将 Evidence micro Precision 从 `0.4103` 提升至 `0.9275`，平均工具调用从 `3.6` 增至 `5.4`；RCA Top-1 没有提升。
- 16 个 Loghub structured CSV 共审计 `32,000` 行，接入审计错误 `0`；无年份的时间格式明确使用行号顺序兜底。
- 当前本机回归为 `110 passed, 1 skipped`；跳过项是只在“未安装可选 SDK”环境适用的失败分支。
- 未配置 API Key 时，`live-benchmark` 明确拒绝运行且不落盘，因此没有伪造真实模型指标。

这些数字属于当前确定性合成分布和本地机器；每次修改数据、规则或依赖后都应重跑，最终以 `outputs_p0/result.json` 和 Manifest 为准。

## 2. 明确边界：当前不使用 RAG，也不使用知识图谱

### 2.1 没有 RAG

当前项目没有向量数据库、Embedding、文本切块、相似度召回、重排序或“把历史事故检索进 Prompt”的流程。

`logrca/diagnostic_signatures.py` 中的 `CAUSE_SIGNATURES` 是一个小型、可审查的本地语义规则基线；`logrca/store.py` 中的 Runbook 是按根因 code 精确查表。二者都不是 RAG：

- 没有把文档编码成向量；
- 没有语义检索索引；
- 没有 Top-K 文档召回；
- 没有用检索结果扩写模型上下文。

这样设计是为了先独立验证 Agent loop、工具边界、证据引用、未知故障拒答和评测链路。如果此时加入 RAG，出现效果变化时会难以判断究竟来自 Agent 编排、检索质量还是知识库内容。

未来只有在具备足量、合法、脱敏的历史工单和 Runbook，并能单独评测检索 Recall@K、引用正确率和知识时效性后，才值得增加 RAG。那会是 P0 之后的新变量，不能倒写成当前已经实现。

### 2.2 没有知识图谱

当前项目没有 Neo4j、图数据库、图嵌入、图查询语言或图推理算法。`logrca/correlation.py` 做的是基于 correlation ID 与时间窗口的事故分组，不是知识图谱。

P0 暂不使用知识图谱的原因是：

1. 当前最先要解决的是日志标准化、事故边界、证据泄漏和可评测性；
2. 静态服务拓扑无法单独证明动态故障因果；
3. 图谱不能自动解决脏日志、缺失链路 ID、错误时间顺序和无依据结论；
4. Demo 数据规模下引入图数据库会增加系统复杂度，却不一定增加可验证收益。

如果以后能从 CMDB、服务注册中心和 OpenTelemetry 获得稳定拓扑，可以把它作为一个只读工具提供给 Agent，用于缩小候选范围；仍然需要时间证据、日志证据和人工确认，不能把“图上相邻”直接当作因果。

面试时可以直接说：

> 当前版本有意不使用 RAG 和知识图谱。P0 先用可审查的语义签名、精确 Runbook 查表和有界日志工具验证 Agent 编排与证据闭环；等历史知识规模和检索评测条件成熟后，再把 RAG 作为独立可消融模块引入。知识图谱只适合补充稳定拓扑，不代替动态因果证据。

## 3. P0 新增能力清单

| P0 能力 | 主要源码 | 当前状态与边界 |
|---|---|---|
| 标签隔离评测语料、真值侧车与 Manifest | `logrca/blind_data.py` | 已实现、确定性生成；仍是本项目内合成基准，不是外部生产盲测 |
| 分组 train/dev/test 切分 | `logrca/blind_data.py` | 已实现；同一故障族的完整措辞变体不会跨 split |
| 正常、已知故障、OOD 未知故障 | `logrca/blind_data.py` | 已实现，用于检测、RCA 与拒答评测 |
| 噪声和扰动 | `logrca/blind_data.py` | 已实现，见本文第 5 节 |
| 通用评测指标与 Bootstrap 区间 | `logrca/benchmarking.py` | 已实现；区间描述当前合成样本，不代表生产置信区间 |
| P0 端到端离线 runner | `logrca/p0_runner.py` | 已实现；含 direct 缺失安全指标与 trace 重新关联子评测 |
| Single/Multi 消融对比 | `logrca/p0_runner.py` | 已实现；比较效果指标与工具调用、延迟代价 |
| 本地单文件批处理 | `logrca/pipeline.py` | 已实现 Spring/Logback 与 Loghub CSV；同步、本地、非流式 |
| Loghub 接入契约审计 | `logrca/ingestion_audit.py` | 已实现、只读；衡量字段覆盖，不等于完成所有数据集的 RCA |
| 重复真实模型 benchmark runner | `logrca/live_benchmark.py` | 代码与 fake runner 测试已实现；没有真实 Key 的运行不能算真实模型结果 |
| OpenAI Agents SDK 可选适配 | `logrca/openai_adapter.py` | 已实现适配边界；模型效果、Token、成本和延迟必须实际运行后再报告 |
| P0 CLI 入口 | `logrca/cli.py` | 已实现 `analyze`、`p0-benchmark`、`live-benchmark`、`audit-loghub` |
| P0 回归测试 | `tests/test_*` | 已覆盖盲测、泄漏、指标、批处理、接入审计和 live runner 控制逻辑 |

## 4. 当前 Agent 架构到底是什么

### 4.1 确定性代码负责有标准答案的工作

- `normalizer.py` / `spring_parser.py`：解析和字段统一；
- `correlation.py`：构建有界 Incident；
- `detector.py`：低成本异常门控；
- `store.py`：提供有界只读工具；
- `security.py`：基础脱敏和指令型日志标记；
- `runtime.py`：运行循环、工具白名单、步数限制、Trace；
- `agents.py::IncidentManager`：固定编排顺序和最终 Grounding；
- `benchmarking.py`：指标计算。

这些工作不交给 LLM，是因为解析、计数、权限、引用存在性和最大返回量都应该可重复、可测试。

### 4.2 Agent 负责不确定的调查决策

本项目中的 Agent 由以下部分组成：

```text
Agent = Instructions + Policy/Model + State + Tools
      + Agent Loop + Structured Output + Guardrails + Trace
```

默认的 `OfflineHypothesisPolicy` 与 `OfflineEvidenceAuditPolicy` 是确定性教学/回归策略，不是大模型。它们真实走过：

```text
读取 State
→ 选择 ToolAction
→ Runtime 验证白名单并执行工具
→ Observation 回填 State
→ 再次决策
→ 返回 FinalAction
```

因此离线结果能证明 Runtime、工具、状态、Schema、证据校验和评测链路可以工作，但不能证明 LLM 对未知生产日志的语义泛化能力。

### 4.3 Single 与 Multi 的差异

`single`：

```text
Gate → Hypothesis Agent → 代码级引用校验 → Report
```

`multi`：

```text
Gate → Hypothesis Agent → Evidence Auditor
     → accepted evidence 与候选 evidence 取安全交集
     → 代码级引用校验 → Report
```

Hypothesis Agent 偏召回，Evidence Auditor 偏精确。Auditor 不允许创造新根因，只能核验候选证据。多 Agent 是否值得，必须查看同一盲测集上的指标增益和额外工具调用/延迟，而不能只因为简历上更亮眼。

### 4.4 真实模型路径如何替换离线策略

`logrca/openai_adapter.py` 使用可选 OpenAI Agents SDK 创建 Hypothesis Agent 和 Evidence Auditor，但 Python Manager 仍然控制执行顺序、结构化输出验证和最终证据交集。这样真实模型不能仅通过一段 Prompt 绕过审核结果。

官方 OpenAI 文档也将工具作为模型能力的扩展，而应用仍需配置工具并处理工具执行边界：[Using tools](https://developers.openai.com/api/docs/guides/tools)。本项目的本地 Runtime 正是在教学环境中显式展示这条边界。

## 5. 盲测如何避免“拿答案做题”

### 5.1 三份物理分离的产物

一次 P0 benchmark 会导出：

- `incidents.agent.jsonl`：Agent 可见事故，不含真值；
- `truth.sidecar.json`：只供评测层使用的标签；
- `manifest.json`：seed、split、场景/扰动分布与内容 SHA-256；
- `result.json`：诊断完成后生成的指标、分层结果、失败样本和消融结果。

`truth.sidecar.json` 存在于同一项目中是为了离线可复现，不是安全隔离区。可靠性来自程序接口和测试确保 Manager 从未接收真值，而不是假设开发者看不到文件。

### 5.2 不透明标识与开发者合成规范

Agent 可见的 incident、event、trace 和 template ID 都是不透明标识，不能通过 `db_pool_exhausted` 之类的名字直接猜答案。真实 Loghub 背景事件进入盲测语料时，只复制 message、service 和规范化 level，不复制其原始 label、line ID、template ID、source 或 metadata。

生成器不导入离线诊断的 `CAUSE_SIGNATURES`，运行时也不会把 sidecar 交给 Manager；服务名使用所有故障族共享的中性名称，时间戳通过 seed 哈希化，避免从拓扑或时间模数反推答案。

但这仍不是“独立外部数据”：盲测措辞和正则签名在同一仓库、同一开发过程中设计，签名明确覆盖当前支持的故障语义。代码隔离能防止显式答案泄漏，不能消除开发者对测试分布的先验。因此这里应称为**标签隔离的开发者合成 benchmark**，不能用它证明未知生产分布上的模型泛化。

### 5.3 分组切分

每个故障族按“完整措辞变体”形成 group；同一文案的两个中性服务版本必须进入同一个 split。测试会同时检查 group 不跨 split，以及 root-evidence message/template ID 在 train、dev、test 间没有精确交集。默认 120 案例为 40/40/40，而不是为了得到更漂亮分数而随机拆日志行。

需要注意：默认离线策略并没有在 train split 上训练，因此这里的 split 主要用于建立未来 Prompt/模型调优时的纪律，以及检查措辞和服务组合隔离。面试中不要把它说成已经完成模型训练。

### 5.4 当前盲测仍然不是生产盲测

P0 的 blind corpus 仍由本项目合成，开发者知道故障族设计；它比早期“Agent 与评测共用模板 ID”的回归集更严格，但仍不能替代：

- 来自真实事故工单的独立标签；
- 按时间切分的生产 holdout；
- 来自不同团队的标注与复核；
- 未知服务、未知版本、未知日志格式上的外部分布测试。

因此应表述为“防显式标签泄漏的合成盲测”，不要简称成“生产盲测”。

## 6. 扰动测试在测什么

`blind_data.py` 使用 seed 确定性地注入扰动，同一个 seed 应得到同一个 Manifest hash。

| 扰动 | 含义 | 主要验证点 |
|---|---|---|
| `distractor_fault` | 插入另一个看起来像故障的错误 | Agent 会不会被显眼但无关的 ERROR 带偏 |
| `missing_evidence` | 删除一条标注证据 | 证据不完整时是否降置信或暴露失败 |
| `missing_direct` | 被删的是 direct 证据 | 只剩支持/症状时是否避免过度确定 |
| `missing_support` | 被删的是 support 证据 | direct 证据存在时能否仍给出谨慎判断 |
| `duplicate_event` | 重放一条日志 | 是否把重复行误当成独立证据 |
| `wrong_trace` | 给一条事件注入错误 trace ID；异常案例强制移动 direct 或 support 证据 | 在独立子评测中重新调用 `build_incidents`，检查事件守恒、被移动证据角色与根因恢复 |
| `out_of_order` | 打乱输入排列 | 是否使用 timestamp，而不是输入顺序推断因果 |

另外还有：

- 正常案例，包括“出现 timeout/reset，但随后明确恢复”的难负样本；
- 已知故障族，用于根因 Top-K；
- OOD 未知故障，用于测试是否 abstain，而不是强行映射到已有分类；

Manifest 会记录 `trace_corruption_role_counts`，结果的 `trace_corruption_role_metrics` 也按 `direct`、`support`、`non_evidence` 分层报告案例数、总体恢复率和已知根因恢复率，避免用随机移动一条无关日志制造“看似通过”的 trace 鲁棒性测试。
- 可选真实 Loghub message 背景噪声。

扰动指标不是为了追求全部满分。失败样本会被保留在 `result.json` 的 `case_failures` 中，按 `misclassification`、`wrong_root_cause`、`incomplete_evidence` 和 `execution_error` 分类。一个困难样本执行失败时，runner 会把它作为 abstaining prediction 继续计分，而不是删除后抬高平均值。

普通 RCA Top-1 会把“direct 证据已删除后安全弃权”计为错误，所以 P0 同时报告三个互补视角：无条件 RCA、`visible_direct_evidence_rca`、`counterfactual_safety`。不要只挑其中最高的一个写进简历。`correlation_resilience` 另行报告被破坏 trace 重新分组后的事件守恒、已知根因恢复、unknown 不乱报与正常不告警；其额外运行不混入基础延迟。

## 7. 指标应该怎样读

不要把文档里的旧数字当作当前结果。离线实际值以本次运行生成的 `outputs_p0*/result.json` 为准，并先检查其中的 `run_config`、`dataset_manifest` 和 `content_sha256`。

### 7.1 Detection

- `precision`：系统判异常的案例中有多少确实异常；
- `recall`：真实异常中有多少被检测出来；
- `f1`：Precision 与 Recall 的综合。

它评估 Incident 级异常门控，不是逐日志行分类。

### 7.2 RCA

- `top1_accuracy`：首个根因候选是否正确；
- `top3_accuracy`：前三个候选是否包含正确根因；
- `mrr`：正确根因排名越靠前得分越高；
- `known_answer_rate`：已知故障中系统实际给出候选的比例。

只有有已知根因标签的异常案例才进入 RCA 分母。正常案例和 OOD unknown 不应该被混入 Top-1。

### 7.3 Unknown/OOD

- `abstention_rate`：未知故障中系统选择“不知道/不在支持目录”的比例。

拒答不是失败；在支持范围外仍高置信给出一个已知根因才更危险。

### 7.4 Evidence

- micro/macro Precision、Recall、F1；
- `exact_set_accuracy`：预测证据集合是否与标注集合完全一致。

这里 `evidence_ids=null` 表示没有做证据标注，应排除在证据指标之外；空列表表示已经标注，并且预期没有证据。二者不能混淆。

### 7.5 Grounding

- `invalid_citation_rate`：引用了 Agent 可见 Incident 之外 ID 的比例；
- `unsupported_claim_rate`：只有存在 claim 支持关系标注时才有意义。

如果当前结果中的 `unsupported_claim_rate` 是 `null`，应解释为“没有可评估的 claim 标注”，不能解释成 0，也不能声称无幻觉。

### 7.6 Efficiency

- 平均工具调用；
- mean、p50、p95 本地延迟；
- 输入/输出 Token；
- 可选成本。

离线策略没有真实模型 Token，所以离线 `average_total_tokens` 和 `average_cost_usd` 可能为 `null`。本地毫秒延迟主要反映 Python 代码执行，不能外推为网络模型延迟或生产 SLO。

### 7.7 Bootstrap 与分层指标

评测对案例做非参数 Bootstrap，给出当前样本上的区间；同时按 `fault_family` 和 `difficulty` 分层。区间只描述该合成语料与采样过程，不证明生产泛化。

### 7.8 从结果文件读取，而不是手抄数字

在 `agent_log_demo` 目录使用 PowerShell：

```powershell
$p0 = Get-Content -Raw -Encoding UTF8 outputs_p0\result.json | ConvertFrom-Json
$p0.run_config | ConvertTo-Json -Depth 6
$p0.dataset_manifest | ConvertTo-Json -Depth 6
$p0.modes.single.evaluation.metrics | ConvertTo-Json -Depth 10
$p0.modes.multi.evaluation.metrics | ConvertTo-Json -Depth 10
$p0.modes.multi.visible_direct_evidence_rca | ConvertTo-Json -Depth 10
$p0.modes.multi.counterfactual_safety | ConvertTo-Json -Depth 10
$p0.modes.multi.correlation_resilience | ConvertTo-Json -Depth 10
$p0.single_vs_multi_ablation.deltas_vs_baseline.multi | ConvertTo-Json -Depth 10
$p0.modes.multi.case_failures | ConvertTo-Json -Depth 10
```

仓库中若同时存在 `outputs_p0/result.json` 和 `outputs_p0_all/result.json`，不能凭目录名猜运行范围；必须读取各自的 `run_config.split` 与 `selected_cases`。通常 test split 用于主要盲测结论，all split 更适合作为完整回归和失败分析。

## 8. 本地批处理的范围与边界

`logrca.pipeline.analyze_path` 把本地文件处理串成：

```text
detect format → parse → correlate → diagnose each Incident
→ save per-Incident reports/traces → save run summary
```

它支持：

- Spring/Logback 文本；
- Loghub structured CSV；
- 自动格式判断或显式指定格式；
- `limit` 限制输入行/CSV record；
- `max_incidents` 限制进入 Agent 的事故数；
- 单个 Incident 失败后记录错误并继续后续 Incident；
- 安全文件名，防止不可信 incident ID 路径穿越；
- 原子写入 JSON，避免中断留下半个文件。

这里的“批处理”是**一个本地文件中多个 Incident 的同步处理**，不是：

- 目录递归消费；
- Kafka/Flink 流处理；
- 分布式 Worker；
- 实时告警平台；
- 生产租户隔离。

`analyze` 命令没有 ground truth，因此输出的是诊断报告和运行统计，不应从它计算或声称 RCA 准确率。

`audit-loghub` 则只读扫描 Loghub structured CSV，统计时间、Level、Service、trace ID、EventId、EventTemplate、LineId、重复 ID 与解析错误覆盖。它回答的是“接入契约覆盖得怎样”，不是“根因诊断效果怎样”。

## 9. 真实模型 benchmark：代码完成不等于实验完成

`logrca/live_benchmark.py` 已实现以下控制逻辑：

- 明确要求模型名；
- 在盲测 corpus 上按故障族做确定性分层抽样；
- 支持 single、multi 或 both；
- 对相同案例重复运行，统计根因稳定性；
- 记录 request、输入/输出 Token、延迟；
- 可选按调用时提供的输入/输出价格估算成本；
- 区分结构化输出失败与普通运行失败；
- 失败仍作为 abstain 进入评测；
- 所有模型调用完成后才关联 truth sidecar。

相关单元测试使用 fake runner 验证这些控制逻辑。fake runner 的 Token、成本、延迟和准确率都是测试夹具，绝不能作为真实模型结果。

如果没有成功使用合法 `OPENAI_API_KEY` 运行 `live-benchmark`，当前项目只能说：

> 已实现可选真实模型适配器和可重复 benchmark harness，但尚未得到真实模型效果、Token、成本、延迟和稳定性结果。

不能说：

- “真实模型 Top-1 达到某数值”；
- “真实模型将 Evidence Precision 提升到某数值”；
- “单个 Incident 成本为某金额”；
- “线上延迟为某毫秒”；
- “Multi 一定优于 Single”。

真实运行后，也应同时记录：

- 完整显式模型名；
- 运行日期；
- seed、split、Manifest hash；
- 样本数和 repeats；
- single/multi；
- 成功、结构化失败、运行失败数；
- Token 与请求数；
- 价格来源和价格日期；
- 机器/网络环境对延迟的影响。

只有同时传入 `--input-price-per-million` 和 `--output-price-per-million` 时才计算成本；价格由运行者提供，代码不会假装知道永远正确的最新价格。未提供价格时 Token 仍可记录，成本应保持 `null`。

## 10. 推荐源码阅读顺序

### 第 0 步：先看最小 Agent loop

1. `run_stage1.py`
2. `agent_core.py::OfflineLogAgent.run`
3. `log_tools.py`

先理解“模型/Policy 只产生动作，Runtime 执行工具并回填 Observation”。

### 第 1 步：理解完整项目的数据契约

4. `logrca/models.py`

重点看 `LogEvent`、`Incident`、`EvidenceRef`、`Hypothesis`、`DiagnosisReport`。Agent 间协作依赖的是这些 Schema，不是自然语言默契。

### 第 2 步：理解日志如何变成 Incident

5. `logrca/spring_parser.py`
6. `logrca/normalizer.py`
7. `logrca/correlation.py`
8. `logrca/detector.py`

重点回答：多行堆栈如何合并、异构字段如何统一、关联 ID 缺失时怎么办、为什么先做异常门控。

### 第 3 步：理解 Agent 的环境和发动机

9. `logrca/store.py`
10. `logrca/runtime.py::AgentRuntime.run`
11. `logrca/security.py`

重点看工具白名单、返回上限、错误 Observation、最大步数和 Trace。工具是能力边界，不只是普通函数列表。

### 第 4 步：理解单/多 Agent 编排

12. `logrca/diagnostic_signatures.py`
13. `logrca/agents.py::OfflineHypothesisPolicy`
14. `logrca/agents.py::OfflineEvidenceAuditPolicy`
15. `logrca/agents.py::IncidentManager.diagnose`

要能解释：离线语义签名不是 RAG，不是知识图谱，也不是 LLM；Auditor 为什么不能创造根因；最终校验为什么必须由代码执行。

### 第 5 步：理解 P0 盲测与评测

16. `logrca/blind_data.py::_make_case`
17. `logrca/blind_data.py::build_blind_dataset`
18. `logrca/benchmarking.py::evaluate_records`
19. `logrca/benchmarking.py::compare_ablations`
20. `logrca/p0_runner.py::run_p0_benchmark`

建议沿这条实际调用链阅读：

```text
cli.main
→ run_p0_benchmark
→ build_blind_dataset
→ _diagnose_mode
→ IncidentManager.diagnose
→ AgentRuntime.run
→ Policy.decide / ToolRegistry.call
→ PredictionRecord
→ evaluate_records
→ result.json
```

### 第 6 步：理解本地批处理和接入审计

21. `logrca/pipeline.py::analyze_path`
22. `logrca/ingestion_audit.py`
23. `logrca/cli.py`

重点区分应用运行、接入审计和有标签 benchmark 是三类不同任务。

### 第 7 步：最后看真实模型适配

24. `logrca/openai_adapter.py`
25. `logrca/live_benchmark.py`
26. `tests/test_openai_adapter.py`
27. `tests/test_live_benchmark.py`

先看稳定的领域边界，再看 SDK，避免只会调用框架却讲不清 Agent 架构。

## 11. 常用命令

以下命令均从 `agent_log_demo` 目录运行。

### 11.1 安装离线依赖并运行测试

```powershell
pip install -r requirements.txt
python -X utf8 -m pytest -q
```

只运行 P0 相关测试：

```powershell
python -X utf8 -m pytest -q `
  tests\test_blind_data.py `
  tests\test_benchmarking.py `
  tests\test_p0_runner.py `
  tests\test_pipeline.py `
  tests\test_ingestion_audit.py `
  tests\test_live_benchmark.py `
  tests\test_cli_p0.py
```

### 11.2 跑一条可读的诊断链路

```powershell
python -X utf8 -m logrca demo --scenario db_pool_exhausted --mode multi
python -X utf8 -m logrca spring --path data\spring_db_pool.log --mode multi
```

### 11.3 跑 P0 test split 盲测

使用仓库中的 Loghub 数据作为去标签背景噪声：

```powershell
python -X utf8 -m logrca p0-benchmark `
  --case-count 120 `
  --seed 20260921 `
  --split test `
  --mode both `
  --bootstrap-iterations 1000 `
  --loghub-root .. `
  --background-per-dataset 50 `
  --output-dir outputs_p0
```

不传 `--loghub-root` 时使用内置中性背景消息。两种运行的输入不同，比较指标时必须先核对 Manifest hash。

### 11.4 跑 all split 完整回归

```powershell
python -X utf8 -m logrca p0-benchmark `
  --case-count 120 `
  --seed 20260921 `
  --split all `
  --mode both `
  --bootstrap-iterations 1000 `
  --loghub-root .. `
  --background-per-dataset 50 `
  --output-dir outputs_p0_all
```

`case_count` 不能小于 100。`all` 适合回归和失败分类；面试中报告盲测结果时，应优先说明 test split，并给出样本数和 hash。

### 11.5 本地批处理一个 Spring 文件

```powershell
python -X utf8 -m logrca analyze `
  --path data\spring_db_pool.log `
  --format auto `
  --mode multi `
  --max-incidents 100 `
  --output-dir outputs\analyze_spring
```

### 11.6 本地批处理一个 Loghub CSV

```powershell
python -X utf8 -m logrca analyze `
  --path ..\BGL\BGL_2k.log_structured.csv `
  --format loghub `
  --mode single `
  --limit 2000 `
  --max-incidents 100 `
  --output-dir outputs\analyze_bgl
```

该命令没有 RCA 真值，只用于验证接入、聚合和诊断流程。

### 11.7 审计全部 Loghub structured CSV 的接入覆盖

```powershell
python -X utf8 -m logrca audit-loghub `
  --root .. `
  --limit 2000 `
  --output outputs\ingestion_audit.json
```

### 11.8 可选真实模型 benchmark

```powershell
pip install -r requirements-openai.txt
$env:OPENAI_API_KEY="<你的合法 API Key>"

python -X utf8 -m logrca live-benchmark `
  --model <你明确选择并记录的模型名> `
  --case-count 120 `
  --seed 20260921 `
  --split test `
  --mode both `
  --max-cases 20 `
  --repeats 3 `
  --max-turns 12 `
  --bootstrap-iterations 1000 `
  --loghub-root .. `
  --background-per-dataset 50 `
  --output outputs\live_benchmark.json
```

不要把 API Key 写入源码、文档、命令历史截图或提交记录。缺少 Key 时命令会明确退出，并且这不算完成真实模型实验。

若要估算成本，必须同时传入由运行者核实的当期价格：

```powershell
  --input-price-per-million <输入价格> `
  --output-price-per-million <输出价格>
```

## 12. 面试高频回答

### 问：这个项目为什么叫 Agent，不只是规则系统？

> 应用整体是混合架构。解析、关联、门控、权限和 Grounding 是确定性 Workflow；诊断阶段通过 State、Policy、Tools、Observation 和受控终止形成 Agent loop。默认 Policy 是离线可复现基线，用于测试编排；真实模型可通过同一结构化边界替换。不会把离线 if/else 冒充成 LLM 推理。

### 问：为什么不用 RAG？

> 当前没有足量且合法的历史工单知识库，也没有检索标签。P0 先用有界日志工具和精确 Runbook 查表验证证据闭环，避免把检索质量与 Agent 编排混在一起。未来会在有独立 Retrieval Eval 后，把 RAG 作为可消融工具加入。

### 问：为什么不用知识图谱？

> 图谱擅长稳定依赖关系，但不能自动证明一次事故中的动态因果。当前优先解决 Incident 边界、时间证据、盲测与 Grounding；未来可把 CMDB/OTel 拓扑作为只读候选缩小工具，而不把图上相邻等同于根因。

### 问：你怎么防止合成数据标签泄漏？

> Agent corpus、truth sidecar 和 Manifest 分离；Agent 可见 ID 不透明；真实 Loghub 背景会剥离标签和原始标识；同一 variant/service group 不跨 split；Manager 接口只接收 Incident，预测完成后评测层才 join truth，并有 spy 测试检查真值字段从未进入 Manager。

### 问：Multi-Agent 的收益是什么？

> Hypothesis Agent 负责召回候选，Evidence Auditor 负责保守审证。是否值得看 `result.json` 中 single/multi 的 Evidence、RCA、工具调用和延迟消融；不能预设 Multi 一定更好。离线实际数值从对应输出文件读取。

### 问：P0 指标能证明生产效果吗？

> 不能。它证明的是在防显式泄漏的合成语料和扰动条件下，工程链路可复现、失败可观察、指标可计算。生产效果还需要脱敏真实事故、时间切分 holdout、跨版本数据和人工复核。

### 问：真实模型测过了吗？

> 代码提供真实模型适配器和重复 benchmark harness；是否实际测过只以成功生成的 live benchmark artifact 为准。没有合法 Key 的情况下，不报告真实模型准确率、Token、成本、延迟或稳定性。

### 问：为什么本地批处理不直接逐行调用 Agent？

> 先由普通代码解析和关联成有限 Incident，再由异常门控过滤正常流量，只有候选事故进入 Agent。这样降低 Token 和成本，也避免模型在几万行日志中自行做本可确定完成的计数与过滤。

## 13. P0 能证明与不能证明的事情

### 可以证明

- 一条本地日志能够经过解析、关联、门控、诊断、校验和落盘；
- Agent 工具访问是有界、只读、可追踪的；
- Single 与 Multi 可以在同一语料上做受控消融；
- 标签不会作为参数进入离线 Manager；
- 缺失、重复、乱序、错误 trace 和干扰故障会进入回归测试；
- 未知故障可以单独评估 abstention；
- 失败案例不会被静默丢弃；
- 真实模型 benchmark 的采样、重复、遥测和失败处理已有可测试接口。

### 不能证明

- 已经在公司生产环境上线；
- 对真实未知故障有当前离线指标所显示的泛化能力；
- 合成 blind test 等同于独立生产盲测；
- 离线 Policy 是 LLM；
- 已完成 RAG 或知识图谱；
- Loghub 的行级标签能提供完整 RCA 真值；
- 真实模型已经达到某个准确率、Token、成本或延迟；
- Multi-Agent 在所有场景都优于 Single-Agent；
- 结构化输出通过就意味着事实正确。

## 14. 后续学习时的验收标准

学完 P0 源码后，应该能脱离文档回答：

1. Agent、Workflow 和 Tool calling 的关系是什么？
2. 为什么 Agent 不能直接拿到 truth sidecar？
3. `AgentRuntime.run` 如何在动作、工具结果和终止之间循环？
4. 为什么工具需要白名单、数量限制和结构化错误？
5. Single 与 Multi 的唯一结构差异在哪里？
6. Evidence Auditor 为什么不能新增根因？
7. Grounding 的“引用存在”与“语义支持”有什么区别？
8. 正常、已知异常、OOD unknown 分别进入哪些指标？
9. 为什么 `unsupported_claim_rate=null` 不能说成没有幻觉？
10. 为什么离线 Token 和成本不能硬填为 0？
11. `analyze`、`audit-loghub`、`p0-benchmark` 各解决什么不同问题？
12. 当前为什么明确不使用 RAG 和知识图谱？
13. 什么证据出现后，才可以说真实模型 benchmark 已经完成？
14. 如何把本地同步 Demo 映射为 Kafka/Flink/日志存储/人工审批的生产架构，而不冒充已经实现？

如果这些问题可以结合具体类、函数、输出文件和失败案例回答，才算真正掌握了这个项目，而不是只记住“多 Agent 日志诊断”这个标题。
