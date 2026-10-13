# LogRCA 系统架构

LogRCA 将文件接入、事件关联、Agent 诊断和证据展示组织为一条本地处理管线。网页与 CLI 共用文件分析入口，内部使用统一的数据契约。

## 数据契约

核心模型位于 `logrca/models.py`：

| 模型 | 作用 |
|---|---|
| `LogEvent` | 规范化日志事件，包含 ID、时间、等级、服务、消息、trace ID 和来源元数据 |
| `Incident` | 当前诊断范围内的日志集合 |
| `Hypothesis` / `EvidenceRef` | 根因候选及其引用证据 |
| `AuditResult` | 审核接受或拒绝的证据 |
| `DiagnosisReport` | 异常判断、根因假设、证据、建议和限制 |

Spring 解析器保留事件的物理起止行号；Java 堆栈续行合并到同一事件。网页通过证据 ID 查回这些元数据和源文本摘录。

## 文件处理

```text
文件 → 格式识别 → 解析与标准化 → 关联为 Incident
     → 异常门控 → 诊断与审核 → 报告、Trace 和运行摘要
```

`pipeline.analyze_path` 支持 Spring/Logback 文本、ECS/Logstash JSONL 与 Loghub structured CSV。关联优先使用 trace/request/block ID，无关联 ID 时采用时间窗。

网页入口 `file_app.analyze_upload` 校验编码、文件大小和扩展名，将文件写入临时目录后调用共享管线。分析结束后删除临时原文件，按独立运行目录保存输出；证据视图使用同一文件的解析结果。

## 诊断后端

### 离线策略

`agents.py` 中的 `OfflineHypothesisPolicy` 和 `OfflineEvidenceAuditPolicy` 使用规则与语义签名决策。策略在 `AgentRuntime` 中返回工具动作或最终动作，工具结果回填运行状态，形成可复现的多轮流程。

### DeepSeek

`deepseek_adapter.py` 使用 `urllib` 请求 DeepSeek。每位专家拥有独立的消息历史，工具请求由 Python 校验和执行，并用原始 `tool_call_id` 回传结果。模型最终 JSON 由 Pydantic 校验。

`DeepSeekIncidentManager` 控制根因专家与证据审核专家的执行顺序。`single` 模式仅运行根因专家，`multi` 模式增加审核专家。正常且门控未触发的事故直接返回报告，不发起模型请求。

### 可选 SDK 适配

`openai_adapter.py` 提供 OpenAI Agents SDK 适配。领域模型、Store、证据过滤和报告契约与其他后端共用；该入口需要额外依赖和模型配置。

## 工具边界

工具从当前 `IncidentStore` 查询数据：

- `get_incident_summary`：事故概要。
- `search_logs`：按等级、服务或关键词筛选。
- `get_timeline`：候选事件的时间顺序。
- `get_events_by_ids`：按引用 ID 回查事件。
- `get_template_stats`：模板频次。
- `get_runbook`：按根因代码读取排查建议。

运行时检查注册工具和允许列表，工具端校验参数并限制结果条数。DeepSeek 路径进一步限制单次工具结果的行数和字符体积。工具只读，不接受任意 Shell、HTTP 或数据库写操作。

## 证据校验

报告组装由代码完成。DeepSeek 多 Agent 路径要求最终引用满足：

```text
当前事故中存在
    ∩ 根因专家已通过工具读取
    ∩ 根因候选引用
    ∩ 审核专家接受
```

证据文本从 Store 重建，并进行基础脱敏。缺少合格直接证据时，根因保留为 `unknown`。结构验证负责字段合法性；审核负责支持关系判断，两者与引用存在性检查分别执行。

## 运行预算与失败记录

DeepSeek 的默认预算针对每个事故：

| 项目 | 默认值 |
|---|---:|
| 两位专家合计模型请求 | 12 次 |
| 每位专家请求 | 6 次 |
| 每个模型响应的工具调用 | 4 个 |
| 单次输出 | 1800 Token |
| 单次网络超时 | 30 秒 |
| 事故后续请求时间预算 | 180 秒 |
| 已报告总用量的停止阈值 | 20000 Token |

已报告用量达到阈值后停止追加请求；最后一次请求可能使累计用量超过阈值。网络错误、协议错误和预算耗尽会记录失败及 Trace。结构验证失败最多执行一次修正请求。

## 可观测性与导出

`TraceRecorder` 记录门控、模型响应、工具参数、工具结果、候选、审核及引用过滤。DeepSeek 运行摘要记录请求尝试数、完成响应数、服务端模型名称、Token 和耗时；缺失的服务端用量单独计数。

网页导出 ZIP 包含 `run-summary.json`、诊断报告、`source-evidence.json` 和 Trace。基础脱敏后的证据摘录用于核对引用，物理行号用于定位使用者持有的原始日志文件。

## 评测

离线评测由 `benchmarking.py` 和 `p0_runner.py` 执行。Agent 只接收事故输入，真值侧车在诊断结束后交给评测器。指标包含检测、RCA、证据精确率与召回率、无效引用、未知故障弃权、工具调用及延迟。

数据和指标定义见 [EVALUATION](EVALUATION.md)。
