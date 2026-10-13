# 智能日志根因诊断 Agent：Demo 架构说明

> 本文描述的是当前仓库中**已经可以离线运行、测试和评测**的教学型 Demo。它的目标是把 Agent 的关键组成讲清楚并形成一条可演示的诊断链路，而不是假装已经解决真实生产环境中的所有日志异常。

## 1. 问题定义与设计目标

输入不是“一条报错”，而是一组可能跨服务、跨层级、互相传播的日志。例如：数据库连接池先耗尽，订单服务随后报 SQL 异常，网关最后返回 503。最晚、最显眼的错误不一定是根因。

本 Demo 要输出一个结构化的 `DiagnosisReport`：

- 是否异常，以及异常门控分数；
- 疑似根因、置信度和影响服务；
- 可回查到原始日志行的证据链；
- 备选假设、下一步排查和处置建议；
- 结论的限制条件；
- Agent 调用了哪些工具，以及完整执行 Trace。

它同时满足四个教学目标：

1. 能看到 `观察 -> 决策 -> 工具调用 -> 新观察 -> 终止` 的 Agent loop；
2. 能比较单 Agent 与 Manager + 专家 Agent 的取舍；
3. 用强类型数据契约和代码级校验约束幻觉；
4. 在没有 API Key 时也能复现、测试和评测。

非目标包括：训练一个通用日志大模型、直接接管生产修复操作、声称合成数据上的指标代表生产效果，以及在 Agent 上下文中加载全部日志。

## 2. 总体数据流

```text
原始日志 / Loghub CSV / 合成 Spring 日志
                    │
                    ▼
       Normalizer：统一为 LogEvent
                    │
                    ▼
       Correlator：trace_id 优先，时间窗兜底
                    │
                    ▼
       Incident：一次待诊断的有界事件集合
                    │
                    ▼
       Anomaly Gate：廉价、可解释、先过滤正常流量
             │正常                 │异常
             ▼                     ▼
        直接返回正常报告      Incident Manager
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                 Hypothesis Agent       Evidence Auditor
                  生成根因假设            独立核验证据
                         └──────────┬──────────┘
                                    ▼
                         代码级 Grounding 校验
                                    │
                                    ▼
                      DiagnosisReport + JSONL Trace
                                    │
                                    ▼
                         CLI / Streamlit / Benchmark
```

这里最重要的边界是：确定性的数据处理不交给大模型。解析、过滤、排序、关联、权限检查、数量限制和引用校验都应由代码完成；Agent 负责在不完整信息下选择下一步调查动作，并综合证据形成假设。

## 3. 核心数据契约

所有核心结构定义在 `logrca/models.py`，使用 Pydantic 做运行时校验。

### 3.1 `LogEvent`

不同来源的日志统一成：

- `line_id`：稳定、可引用的证据 ID；
- `timestamp`：统一时间类型；
- `service`、`level`、`message`；
- `template_id`、`event_template`：用于压缩和模式识别；
- `trace_id`：用于跨服务关联；
- `source`、`metadata`：保留来源及扩展信息。

Agent 最终只保存 `line_id` 等短引用，而不是在各 Agent 间反复复制整段日志。这既减少 Token，也让引用能够被程序回查。

### 3.2 `Incident`

`Incident` 是诊断的工作单元。它将一次请求链或一个时间窗口内的有限日志封装在一起。Agent 不能无边界地查询整个日志库，而只能通过工具访问当前事故。

### 3.3 `Hypothesis` 与 `EvidenceRef`

根因假设和证据是分开的。证据分为四类：

- `direct`：直接描述底层失败，例如 “Hikari 连接不可用”；
- `support`：与直接原因相互印证，例如后续 SQL 连接失败；
- `symptom`：级联现象，例如网关 503、重试、熔断；
- `contradiction`：引用不存在，或与假设矛盾。

每条证据必须携带 `line_id`。文本看似合理但没有合法 ID 的内容，不能成为最终证据。

### 3.4 `DiagnosisReport`

报告是系统的稳定输出边界。无论内部使用离线规则策略还是可选真实模型，调用方都只依赖同一个 Schema。这样可以替换 Agent 实现，而不影响 CLI、UI 和评测程序。

## 4. 组件职责

| 组件 | 当前职责 | 为什么不交给 Agent |
| --- | --- | --- |
| `normalizer.py` | 读取 Loghub 结构化 CSV、解析时间、统一字段、提取请求/块 ID、计算模板表示压缩率 | 格式转换是确定性工作；让模型解析既贵又不稳定 |
| `spring_parser.py` | 解析明确的 demo Logback layout，把 Java 堆栈续行合并到首行事件，并生成模板基线 | 展示 raw text → LogEvent 全链路；不宣称一个正则兼容所有企业格式 |
| `correlation.py` | `trace/request/block ID` 精确分组；无 ID 时按时间窗分组；限制单事故最大日志数 | 控制上下文边界，避免一个异常 ID 吞入无限日志 |
| `detector.py` | 在 Agent 之前做异常门控 | 大多数正常流量不值得消耗模型调用 |
| `store.py` | 保存事故并暴露有界只读工具 | 限制 Agent 数据访问范围和返回条数 |
| `security.py` | 基础秘密脱敏、标记日志中的指令式文本 | 日志是不可信数据，不能被当成提示词指令 |
| `runtime.py` | 执行 Agent loop、工具注册、步数上限、Trace | 将编排机制与具体诊断策略解耦 |
| `agents.py` | Manager、根因假设策略、证据审核策略、最终报告组装 | 负责有不确定性的调查与推理 |
| `evaluation.py` | 合成 RCA 指标、单/多 Agent 对照、BGL 行级基线 | 评测标签不能泄露给诊断 Agent |
| `openai_adapter.py` | 可选 OpenAI Agents SDK 实现 | 把变化较快的模型/SDK 与稳定领域管线隔离 |

## 5. Agent 到底由哪些部分组成

在本项目中，一个 Agent 不是“一个类加一段 Prompt”，而是以下部分的组合：

1. **目标与指令**：定义角色、任务边界、允许的行为和停止条件；
2. **状态 `AgentState`**：当前任务、已经获得的观察和步数；
3. **策略 `Policy.decide`**：根据状态决定调用哪个工具或返回最终结果；
4. **工具 `ToolRegistry`**：Agent 与外部确定性系统交互的唯一入口；
5. **记忆/上下文**：本 Demo 是当前一次运行中的 `observations`，没有实现长期记忆；
6. **输出 Schema**：例如 `HypothesisBundle` 和 `AuditResult`；
7. **运行时 `AgentRuntime`**：负责循环、异常封装、上限控制和终止；
8. **Guardrail**：工具限流、日志脱敏、证据 ID 校验、最大步数；
9. **Trace 与评测**：记录可观察动作，并用标签衡量结果。

可把 Agent loop 写成：

```text
state = {task, observations=[], step=0}
while step < max_steps:
    action = policy.decide(state)
    if action is FinalAction:
        return typed_output
    observation = bounded_tool(action.name, action.arguments)
    state.observations.append(observation)
raise MaxStepsExceeded
```

本 Demo 的默认 `Policy` 是确定性规则策略。它不是 LLM，但完整走过同一种 Agent 运行机制，因此离线可测、易读、没有 API 成本。`openai_adapter.py` 则展示真实模型如何复用同一领域工具与输出契约。

## 6. Manager + 两个专家的协作方式

### 6.1 Incident Manager

Manager 是工作流所有者，职责是：

- 先运行异常门控；
- 决定是否值得调用诊断 Agent；
- 调用根因假设专家；
- 在 `multi` 模式下调用证据审核员；
- 执行最终代码级引用校验；
- 合并 runbook、影响服务、限制条件并输出报告。

Manager 不把最终控制权交给专家。专家输出只是受约束的中间数据，Manager 对最终答案负责。这是 `manager-as-tools` 思路，而不是专家之间任意闲聊。

### 6.2 Hypothesis Agent

根因假设 Agent 的实际步骤是：

1. `get_incident_summary`：先看规模、服务、等级分布；
2. `search_logs`：只检索 ERROR/CRITICAL/WARN，避免读取全部日志；
3. `get_timeline`：按时间排序，寻找“最早直接故障 -> 支持证据 -> 下游症状”；
4. `get_runbook`：从维护过的手册取得排查与处置建议；
5. 返回强类型 `HypothesisBundle`。

它负责“召回候选”。因此默认会把一些级联症状保留为上下文，证据更全，但精确度未必最高。

### 6.3 Evidence Auditor

审核 Agent 不允许创造新根因，只做保守核验：

1. `get_events_by_ids`：检查每个引用是否属于当前事故；
2. `get_timeline`：复核先后关系；
3. 保留直接证据和支持证据；
4. 剔除网关 5xx、重试、熔断等症状型证据；
5. 如果没有直接证据，降低置信度并写入审计说明。

这是有意设计的职责分离：一个角色偏召回，一个角色偏精确。只有当这种分工能被评测证明有价值时，多 Agent 才值得引入。

## 7. 单 Agent 与多 Agent 对照

### `single` 模式

```text
Gate -> Hypothesis Agent -> 代码级引用校验 -> Report
```

优点是调用少、延迟低、结构简单；缺点是根因假设者同时评价自己的证据，容易保留下游症状。

### `multi` 模式

```text
Gate -> Hypothesis Agent -> Evidence Auditor -> 代码级引用校验 -> Report
```

优点是独立复核、证据更干净；代价是多一次 Agent loop、更多工具调用和更高模型成本。当前合成基准专门验证：两种模式根因均可正确时，多 Agent 应用额外成本换取更高证据精确率。这个结果只证明当前场景中的设计取舍，不证明“多 Agent 天生优于单 Agent”。

实际生产选择应基于失败模式：如果问题只是输出格式不稳定，增加一个 Agent 往往无益；如果根因生成和证据反驳确实需要不同目标、工具或权限，才适合拆分。

## 8. 工具设计与 Token 控制

`IncidentStore` 暴露的是窄工具，而不是“读取全部日志”：

- `get_incident_summary`：只返回统计摘要；
- `search_logs`：可按等级、服务、关键词过滤，最多 100 条；
- `get_timeline`：只排序候选 ID，默认最多 30 条；
- `get_events_by_ids`：按证据 ID 回查；
- `get_template_stats`：返回模板频次；
- `get_runbook`：返回受维护的建议。

主要节省策略是：异常门控减少调用次数；模板 ID 去重减少文本体积；先摘要再检索；只对候选取原文；Agent 间传 ID 和 Schema；限制结果条数与最大步数；正常事故直接结束。

`normalizer.compression_stats` 比较原始文本、逐行 JSON 和“模板字典 + ID 序列”三种表示。它是表示开销实验，不是 gzip 压缩率，也不能直接等同于 Token 节省比例。

## 9. 边界、异常与失败处理

### 数据边界

- 每次诊断只处理一个有上限的 `Incident`；
- 优先用 `trace_id` 关联，无 ID 时使用时间窗口，但时间相近不等于存在因果；
- 日志内容被视为不可信数据，基础敏感字段会脱敏，指令式文本只做安全标记；
- 工具访问只读，Demo 不提供执行命令、发布、扩容或删除数据的能力。
- 每个 `AgentRuntime` 还有独立 `allowed_tools` 白名单：Hypothesis Agent 不能调用审核专用能力，Auditor 也不能越权；权限由运行时代码执行，而不是只写在 Prompt 中。

### 运行时失败

- 未注册工具：抛出明确的 `Unknown tool`；
- 工具运行异常：被转换成观察返回给 Agent，使策略有机会调整；
- 死循环：`max_steps` 达到后强制失败；
- 非法参数：例如检索 `limit > 100` 会被代码拒绝；
- 未识别根因：返回 `unknown`、降低置信度，并要求补充指标/Trace/变更记录；
- 非法引用：最终 Grounding guardrail 删除不属于当前事故的 ID；
- 正常流量：门控未触发时工具调用数为 0。

当前运行时没有实现自动重试、指数退避、断路器、持久化消息队列和跨进程恢复。这些属于生产层能力，而不是被悄悄假定已经存在。

### 模型失败边界

可选真实模型即使输出通过 Schema 校验，也可能事实错误。因此结构化输出解决的是“格式可校验”，不是“事实必然正确”。证据回查、代码级引用校验、置信度降级和人工确认仍然需要保留。

`openai_adapter.compose_audited_report()` 因而是纯代码护栏：最终证据必须同时属于 Hypothesis 候选、Auditor 的 accepted 集合和当前 Incident；摘要重新从脱敏 Store 构建，不能采用模型复制的原文；没有证据时 confidence 上限为 0.35，缺少 direct 证据时上限为 0.55。该函数无需 SDK/网络即可做回归测试。

## 10. 可观测性与评测

每次诊断使用 `TraceRecorder` 写 JSONL，可观察：

- Agent 启动与结束；
- 调用了哪个工具及参数；
- 工具结果的有界预览和耗时；
- 异常门控与最终 grounding 结果。

Trace 记录的是外部可观察动作和摘要，不保存隐藏思维链。

合成评测覆盖：

- 异常 Precision、Recall、F1；
- 根因准确率；
- 证据 Precision、Recall；
- 平均工具调用次数和延迟；
- 单/多 Agent 成本—质量对照。

BGL 公开数据只用于输入适配、压缩实验和行级异常基线。BGL 的 `Label` 不是业务事故根因标签，因此不能拿它声称 RCA 准确率。

## 11. 已实现与未实现

### 已实现并可运行

- 六个可复现 Spring 风格合成场景及隔离的 ground truth；
- 120 个标签侧车 P0 案例，覆盖 8 类已知故障、正常、OOD 和多种扰动；
- 合成事故/事件使用不透明 ID，场景名只存在于评测真值侧，避免真实模型从标识猜答案；
- 16 个 Loghub structured CSV 的适配审计、模板表示统计和基础关联；
- 指定 demo Logback layout 的原始文本解析与 Java 堆栈续行合并；
- 可解释异常门控；
- 通用 Agent runtime、工具注册、最大步数和 Trace；
- 单 Agent / Manager + Hypothesis + Auditor 两种模式；
- 证据 grounding、基础脱敏和提示注入文本标记；
- CLI、Streamlit、自动化测试、离线 benchmark 与本地文件批处理；
- 可选 OpenAI Agents SDK 适配层。

### 仅为接口或设计，尚未生产实现

- Kafka/Flink 实时流处理、Elasticsearch/OpenSearch 检索；
- OpenTelemetry Trace/Metric/Deployment 变更关联；
- 基于历史基线的无监督/序列异常模型；
- 多租户 RBAC、审计、合规 DLP 与密钥管理；
- 分布式任务恢复、缓存、配额与模型网关；
- 真实生产标注集、在线 shadow test 和 SRE 人审闭环；
- 自动执行修复操作。

## 12. 建议源码阅读顺序

1. `logrca/models.py`：先理解系统中的“对象”和边界；
2. `logrca/synthetic.py`：看输入场景与标签如何隔离；
3. `logrca/runtime.py`：理解最小 Agent loop；
4. `logrca/store.py`：理解工具为何必须窄、只读、有上限；
5. `logrca/agents.py`：跟踪单/多 Agent 的完整决策路径；
6. `logrca/detector.py`：理解为什么 Agent 前需要门控；
7. `logrca/evaluation.py`：理解“能跑”与“可评测”的差别；
8. `logrca/normalizer.py`、`spring_parser.py`、`correlation.py`：理解公开 CSV 与原始 Spring 文本接入；
9. `logrca/openai_adapter.py`：最后看如何把规则 Policy 替换为真实模型。

阅读时建议同时打开生成的 Trace。代码回答“系统可以做什么”，Trace 回答“这一次系统实际做了什么”。
