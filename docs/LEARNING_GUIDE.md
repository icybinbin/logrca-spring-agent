# 智能日志诊断 Agent 源码学习指南

> 版本说明：本指南主要讲离线策略和早期 OpenAI 适配器。当前已增加 DeepSeek 工具调用、Spring 文件上传、证据行号核对和结果下载；先按 [QUICKSTART](../QUICKSTART.md) 运行，再看 [README 中的 DeepSeek 实现与学习入口](../README.md)。下文离线评测和旧适配器的限制不能代替当前 DeepSeek 路径的说明或实测。

> 适用对象：第一次自己设计 Agent、希望以求职和面试为导向学习的人。
>
> 这份文档不要求你先懂大模型框架。建议先把默认离线版本跑通，再沿着一条真实执行链读源码，最后才看可选的真实模型适配器。

## 0. 先把项目定位说准确

这个仓库实现的是一个“可运行、可观察、可评测”的智能日志根因诊断 Demo。它覆盖了日志统一建模、事故聚合、异常门控、工具检索、根因假设、证据审核、结构化报告、Trace 和离线评测。

它不是完整的生产系统，也没有使用公司生产数据。默认演示数据是透明的 Spring 风格合成事故；Loghub 的公开结构化 CSV 用于验证数据接入、关联、压缩和行级异常基线。

必须特别记住：

- `OfflineHypothesisPolicy` 和 `OfflineEvidenceAuditPolicy` **不是 LLM**。
- 它们是确定性的 Python 决策策略，用来在没有 API Key、没有模型费用的情况下，把 Agent 的“决策—工具—观察—再决策—结束”骨架完整跑通。
- 离线路径中的 `instructions` 会被放进任务和 Trace，起到职责说明与未来替换接口的作用；离线策略本身并不会阅读或理解这些自然语言指令，它的行为写在 `decide()` 代码里。
- 真正把 `instructions` 交给模型执行的是可选的 `logrca/openai_adapter.py`。该路径需要额外安装 SDK、配置 API Key 并显式指定模型；默认测试不会调用它。

面试时可以这样诚实描述：

> 我做了一个 offline-first 的日志 RCA Agent Demo。默认用可复现策略模拟模型决策，因此核心编排、工具、状态、结构化输出、证据约束和评测都能零成本回归；同时保留 `Policy` 协议和 OpenAI Agents SDK 适配器，可替换成真实模型。公开 Loghub 数据用于接入与行级异常实验，合成微服务事故用于验证根因证据链。我不会把合成评测说成生产效果。

## 1. 先建立最重要的 Agent 心智模型

普通大模型调用通常是：

```text
输入问题 -> 模型一次生成 -> 文本答案
```

Agent 更像一个带工具和停止条件的闭环：

```text
任务 + 当前状态
      |
      v
决策器（模型或 OfflinePolicy）
      |
      +---- 选择工具 ----> 工具执行 ----> 观察结果 ----+
      |                                             |
      +------------------- 再次决策 <---------------+
      |
      +---- FinalAction -> 结构化输出
```

因此，“使用大模型”不等于“做了 Agent”。判断一个系统是不是 Agent，重点看它是否具备以下能力：

1. 根据目标和当前观察选择下一步，而不是预先写死一条纯流水线。
2. 能调用受控工具取得外部信息或执行动作。
3. 工具结果会写入状态，影响下一次决策。
4. 有明确的结束动作、最大步数和失败边界。
5. 最终输出能被程序校验和消费。

本项目既有工作流，也有 Agent：

- `IncidentManager.diagnose()` 是确定性的工作流/编排器，负责异常门控、调用专家和最终落表。
- `AgentRuntime.run()` 是 Agent 循环。
- `OfflineHypothesisPolicy.decide()` 和 `OfflineEvidenceAuditPolicy.decide()` 是离线决策器。
- `openai_adapter.py` 中的 `Agent` 才是可选的真实模型 Agent。

这个区分很适合面试。不要把任何 `if/else` 都包装成“AI Agent”；可以说离线策略是为了测试编排而提供的 deterministic policy，真实模型通过相同边界接入。

## 2. Agent 的九个组成部分，在本项目里分别是什么

常见“Agent 八股文”会说模型、Prompt、工具、记忆和规划。仅背名词不够。下面把每个名词映射到真实源码。

| Agent 部分 | 本项目中的实现 | 作用 |
| --- | --- | --- |
| 决策器 / Model | 离线：`OfflineHypothesisPolicy.decide()`、`OfflineEvidenceAuditPolicy.decide()`；在线：SDK `Agent(model=...)` | 根据任务、历史观察决定调用哪个工具或结束 |
| 指令 / Instructions | `HYPOTHESIS_AGENT_INSTRUCTIONS`、`EVIDENCE_AUDITOR_INSTRUCTIONS`、`MANAGER_INSTRUCTIONS` | 限定角色、目标、证据规则和禁止事项 |
| 工具 / Tools | `IncidentStore` 的查询方法，经 `_tool_registry()` 注册 | 给 Agent 提供受限的数据访问能力 |
| 循环 / Runtime | `AgentRuntime.run()` | 实现 decide → call tool → append observation → decide |
| 状态 / Memory | `AgentState.task`、`AgentState.observations`、`AgentState.step` | 保存本次运行的短期状态 |
| 结构化输出 | `HypothesisBundle`、`AuditResult`、`DiagnosisReport` | 避免 Agent 之间只传不可校验的散文 |
| Guardrail | `max_steps`、工具 limit、`sanitize_log_text()`、最终 line ID grounding | 限制失控循环、越界读取、秘密泄漏和证据幻觉 |
| Trace / Observability | `TraceRecorder`、`TraceEvent` | 记录动作、工具参数、结果预览、耗时和最终节点 |
| Eval / Evaluation | `run_benchmark()`、`compare_modes()`、pytest | 定量检查异常判断、根因、证据质量、成本代理指标 |

下面逐个讲透。

### 2.1 决策器：它负责“下一步做什么”

`runtime.py` 定义了协议：

```python
class Policy(Protocol[OutputT]):
    def decide(self, state: AgentState) -> Action[OutputT]: ...
```

任何对象只要实现 `decide(state)`，返回 `ToolAction` 或 `FinalAction`，就可以挂到 `AgentRuntime` 上。这个设计把“Agent 循环”与“谁来决策”解耦：

- 现在可以使用确定性的 `OfflineHypothesisPolicy`。
- 以后可以写一个调用任意 LLM 的 `Policy`。
- 测试可以用故意永不结束的 `NeverFinishPolicy` 验证步数上限。

这是很重要的架构思想：**稳定的领域层和 Runtime 不应被某个模型供应商绑死。**

### 2.2 指令：不是装饰文字，而是职责边界

`agents.py` 中三个指令常量分别约束：

- 假设专家：先查摘要和时间线，只引用工具返回的 `line_id`，不执行修复。
- 证据审核员：不创造新根因，只核验引用，并区分直接原因与级联症状。
- Manager：控制何时结束、何时调用专家，以及最终输出字段。

在线路径中，指令会真正送给模型。离线路径中，行为由 Python 决策代码实现，指令主要用于说明契约和记录 Trace。面试时如果被问到“为什么 Prompt 写得不长”，可以回答：

> 我把可确定的规则下沉到代码、Schema 和工具权限中。Prompt 只承担角色与决策原则，不承担所有安全责任。Prompt guardrail 之外还有代码 guardrail。

### 2.3 工具：Agent 的“受控手脚”

`IncidentStore` 暴露了：

- `get_incident_summary()`：只返回规模、时间范围、等级和服务统计。
- `search_logs()`：按关键词、级别、服务检索，限制 1～100 条。
- `get_timeline()`：只对候选 `line_id` 排序并返回有限条目。
- `get_events_by_ids()`：审核具体引用是否存在。
- `get_template_stats()`：返回模板频次。
- `get_runbook()`：读取维护好的排查步骤和处置建议。

`_tool_registry()` 把这些方法注册到 `ToolRegistry`。Policy 只说工具名和参数；`AgentRuntime` 才真正执行函数。

为什么不把全部几万行日志直接贴给模型？

- Token 成本高。
- 上下文越长，关键信号越容易被淹没。
- 原始日志可能含密钥或伪装成指令的文本。
- 工具可以限制读取范围，并保留审计记录。
- 模型引用 `line_id`，代码可以反查证据是否真实存在。

### 2.4 循环：Agent 与一次函数调用的核心差别

`AgentRuntime.run()` 每一轮做四件事：

1. 把当前步数写入 `AgentState.step`。
2. 调用 `policy.decide(state)`。
3. 如果返回 `FinalAction`，记录 `agent_final` 并退出。
4. 如果返回 `ToolAction`，记录调用、执行工具、把结果追加到 `state.observations`，进入下一轮。

伪代码可以背成：

```python
while step <= max_steps:
    action = policy.decide(state)
    if action is final:
        return typed_output
    observation = tools.call(action.name, action.arguments)
    state.observations.append(observation)
raise RuntimeError("超过最大步数")
```

`max_steps` 不是小细节，它是最基础的预算和安全边界。模型可能重复调用工具、参数错误后重试，或者永远不产生最终答案；没有硬停止条件的 Agent 无法稳定上线。

### 2.5 状态与记忆：当前 Demo 只有“运行内短期记忆”

`AgentState` 只有三个主要字段：

- `task`：本次任务，例如 `incident_id`、指令、待审核假设。
- `observations`：本轮运行已经取得的工具结果。
- `step`：当前步数。

`_observation(state, tool_name)` 会检查某个工具是否已调用过，从而决定下一步。例如假设 Agent 先看摘要；看到摘要后才检索异常日志；有异常日志后才拉时间线。

这里没有实现长期记忆、向量记忆或跨会话用户画像。Trace 文件也不是自动喂回 Agent 的记忆，它只是观测和审计记录。面试时不要把两者混淆：

- Memory 是后续决策的输入。
- Trace 是运行过程的记录。

### 2.6 结构化输出：让 Agent 的结果可校验

`models.py` 用 Pydantic 定义了稳定契约：

- `EvidenceRef`：证据 ID、类型、摘要、权重。
- `Hypothesis`：候选根因、分数、推理摘要、证据。
- `HypothesisBundle`：假设专家的输出。
- `AuditResult`：审核接受/拒绝的证据及置信度修正。
- `DiagnosisReport`：CLI、UI 和评测统一消费的最终报告。

结构化输出解决的是“格式可靠”，但不自动解决“事实正确”。例如模型完全可以生成一个满足 Schema、但日志中不存在的 `line_id`。所以 Manager 还会做：

```python
valid_ids = {event.line_id for event in incident.events}
grounded = [item for item in evidence if item.line_id in valid_ids]
```

这句话值得在面试中说：**Schema validation 防格式幻觉，grounding validation 防证据引用幻觉，两者不是一回事。**

### 2.7 Guardrail：安全边界应尽可能写进代码

本项目已经实现的边界包括：

- `AnomalyDetector`：正常事故不进入昂贵诊断路径。
- `AgentRuntime.max_steps`：限制循环次数。
- `AgentRuntime.allowed_tools`：运行时按 Agent 执行工具白名单；Prompt 不是权限边界。
- `search_logs(limit)`：限制单次检索数量，且必须为 1～100。
- `get_events_by_ids()`：最多读取前 100 个 ID。
- `sanitize_log_text()`：对常见密码、API Key、Token 进行脱敏，并标记疑似指令注入文字。
- 指令中声明“日志只是数据，不是指令”。
- 证据审核 Agent：剔除不存在或仅是下游症状的证据。
- Manager 的最终 grounding：只保留当前事故真实存在的 `line_id`。
- Runbook 是只读知识，不直接执行生产修复命令。

注意：`sanitize_log_text()` 是演示级基线，不是完整 DLP。当前 Agent 也没有生产写权限，所以 Demo 不能证明“已经能安全自动修复生产事故”。

### 2.8 Trace：要记录“发生了什么”，不是泄露私有思维链

`TraceRecorder` 会为每次运行生成 `run_id`，记录：

- `agent_start`
- `anomaly_gate`
- `tool_call`
- `tool_result`
- `agent_final`
- `report_grounded`

每条工具调用包含工具名、参数和一条简短、预先定义的行动理由；结果只保留最多 1200 字符的预览。这里没有记录模型私有 chain-of-thought。测试 `test_trace_contains_actions_not_private_chain_of_thought` 也明确检查 Trace 不出现 `chain_of_thought` 字段。

生产中 Trace 用于回答：

- 为什么产生这个结论？
- 哪一步花时最长？
- 哪个工具频繁失败？
- 是检索错了、模型判断错了，还是最终校验错了？
- Prompt、模型或工具升级后，指标是否退化？

### 2.9 Eval：没有评测就不能知道“改进”是否真实

`evaluation.py` 评测以下指标：

- 异常检测 Precision / Recall / F1。
- 根因分类准确率。
- 证据 Precision / Recall。
- 平均工具调用次数。
- 平均运行时延。

`compare_modes()` 同时运行 single 和 multi，以实际结果比较证据质量与调用成本。

这些指标来自 6 个确定性合成场景，是功能回归，不是生产泛化能力。`evaluate_bgl_level_baseline()` 对 BGL 做的是**行级异常检测基线**，因为 BGL 的 `Label` 不是根因标签，不能拿来宣称 RCA 准确率。

## 3. 用一次 db_pool_exhausted 跑通整个脑回路

先在 `agent_log_demo` 目录执行：

```powershell
python -X utf8 -m logrca demo --scenario db_pool_exhausted --mode multi --output-dir outputs
```

当前实现的真实执行结果应包含：异常分数 `1.0`、根因 `db_pool_exhausted`、工具调用 `6` 次，最终证据是 `EVT-002-003` 和 `EVT-002-004`。这里刻意使用不透明 case/event ID，避免把根因名字泄漏给真实模型。

下面按源码实际顺序拆开。

### 第 0 步：合成一段有明确真值、但不泄漏给 Agent 的事故

入口在 `synthetic.py` 的 `build_scenario("db_pool_exhausted")`。

事故共有 6 条事件：

| line_id | service | level | template_id | 在因果链中的位置 |
| --- | --- | --- | --- | --- |
| EVT-002-001 | gateway | INFO | HTTP_REQUEST | 请求入口 |
| EVT-002-002 | order-service | INFO | ORDER_CREATE | 正常业务动作 |
| EVT-002-003 | order-service | ERROR | DB_POOL_EXHAUSTED | 直接根因证据 |
| EVT-002-004 | order-service | ERROR | DB_CONNECTION_FAILED | 支持证据 |
| EVT-002-005 | gateway | ERROR | GATEWAY_5XX | 下游/上游可见症状 |
| EVT-002-006 | gateway | WARN | CLIENT_RETRY | 级联症状，可能进一步放大故障 |

`SyntheticCase` 同时含 `incident` 和 `truth`。关键安全线是：

- `incident` 交给 Agent。
- `truth` 只交给评测器。
- `root_cause_code` 和 `evidence_ids` 不会出现在事件列表里。

这避免了评测标签泄漏。测试 `test_scenarios_are_deterministic_and_ground_truth_is_separate` 专门验证这一点。

### 第 1 步：Manager 先做异常门控

`cli.py::main()` 构造场景后调用：

```python
IncidentManager().diagnose(case.incident, mode="multi", trace_dir=...)
```

`IncidentManager.diagnose()` 第一件事不是找 LLM，而是调用 `AnomalyDetector.assess()`。本场景的分数计算为：

```text
3 条 ERROR        = 3 × 0.16 = 0.48
1 条 WARN         = 1 × 0.04 = 0.04
1 个关键故障模板 = 1 × 0.48 = 0.48
错误跨两个服务   =             0.08
合计             =             1.08
封顶后           =             1.00
```

阈值默认是 `0.35`，所以进入诊断路径。若场景为 `normal`，Manager 会直接返回报告，`tool_calls=0`，不会启动两个专家 Agent。

这叫做“cheap gate before expensive reasoning”：能用便宜规则解决的筛选，不要先花 LLM Token。

### 第 2 步：构造工具环境

Manager 把当前事故放进 `IncidentStore([incident])`，再用 `_tool_registry(store)` 注册查询工具。

这里有一个重要隔离：工具调用总是带 `incident_id`，审核引用时也只在当前事故内查询。Agent 并没有任意读取文件系统或执行 Shell 的工具。

### 第 3 步：假设 Agent 第 1 轮看摘要

`AgentRuntime` 初始化：

```python
AgentRuntime(
    "hypothesis_agent",
    OfflineHypothesisPolicy(),
    tools,
    trace,
    max_steps=6,
)
```

第一次 `decide(state)` 发现状态里还没有 `get_incident_summary` 的观察，于是返回：

```python
ToolAction(
    "get_incident_summary",
    {"incident_id": "INC-SYN-002"},
    "先确定事故规模、服务和错误等级分布。",
)
```

工具返回 6 个事件、2 个服务、等级分布和时间范围。Runtime 把结果追加到 `state.observations`。

### 第 4 步：假设 Agent 第 2 轮只检索异常候选

第二次 `decide()` 已看到摘要，但没有 `search_logs` 结果，于是调用：

```text
search_logs(
  levels=[ERROR, CRITICAL, WARN],
  limit=30
)
```

工具只返回 `EVT-002-003` 到 `EVT-002-006`，两条正常 INFO 没有进入后续上下文。这里已经体现第一次 Token 削减。

### 第 5 步：假设 Agent 第 3 轮建立时间线

第三次 `decide()` 将这 4 个候选 ID 交给 `get_timeline()`，按时间得到：

```text
EVT-002-003 连接池获取连接超时
  -> EVT-002-004 SQLTransientConnectionException
  -> EVT-002-005 网关 503
  -> EVT-002-006 客户端重试
```

Agent 不应看到 `503` 就直接说“网关故障”。根因定位关注的是：哪个高解释力事件最早出现，它能否解释后续多个症状。

### 第 6 步：假设 Agent 第 4 轮检索 Runbook

`rank_causes(timeline)` 在 `diagnostic_signatures.py` 中根据 message/event_template 的语义正则、证据数量和时间顺序识别出 `db_pool_exhausted`。它故意不读取 `template_id`，因此把 EventId 全部改成不透明值也不应改变结论。随后调用：

```text
get_runbook(root_cause_code="db_pool_exhausted")
```

排查与处置建议来自维护好的 `RUNBOOKS`，不是让决策器临场编造。这里是普通的按根因 code 精确字典查找，**不是 RAG**：没有 embedding、向量索引、相似度召回、Top-K 文档或重排序。

### 第 7 步：假设 Agent 输出 HypothesisBundle

第 5 次 `decide()` 不再调用工具，而是返回 `FinalAction(HypothesisBundle(...))`。

它把事件分成：

- `EVT-002-003`：`direct`，候选阶段权重 0.96。
- `EVT-002-004`：`support`，候选阶段权重 0.74。
- `EVT-002-005`：`symptom`，候选阶段权重 0.20。
- `EVT-002-006`：`symptom`，候选阶段权重 0.20。

multi 模式审核通过后，direct/support 会规范为 0.98/0.78；symptom 被放进 rejected，不进入最终证据链。

注意“5 轮决策”不等于“5 次工具调用”。最后一轮是结束动作，所以假设 Agent 的工具调用数为 4。

### 第 8 步：multi 模式启动独立证据审核 Agent

如果 `mode="single"`，Manager 会直接使用上述 4 条证据。`mode="multi"` 则把完整假设交给 `OfflineEvidenceAuditPolicy`。

审核员做两次工具调用：

1. `get_events_by_ids()`：验证 4 个 ID 确实存在于当前事故。
2. `get_timeline()`：再次确认时间顺序与模板角色。

然后它只接受：

- `EVT-002-003`：直接证据。
- `EVT-002-004`：支持证据。

并拒绝 `EVT-002-005/006` 作为根因证据，因为它们是网关错误与重试症状。审核 Agent 的工具调用数为 2，所以 multi 模式总调用数是 `4 + 2 = 6`。

审核员不允许创造新根因，这叫做“职责收窄”。多 Agent 不是让每个 Agent 都自由发挥，而是给每个专家更小、更可评测的任务。

### 第 9 步：Manager 用代码做最终 Grounding

即使审核员已经检查过，Manager 仍然重新构建当前事故的合法 ID 集合，只保留真实存在的引用。然后组装 `DiagnosisReport`：

- 异常判断与分数。
- 根因代码与中文标题。
- 置信度 0.93。
- 受影响服务。
- 证据链。
- 下一步排查。
- 处置建议。
- 局限性。
- 工具调用数与 Trace 路径。

这里的 `0.93` 是启发式分数，不是经过校准的真实概率。UI 和 CLI 也明确说明这一点。

### 第 10 步：Trace 落盘

指定 `--output-dir outputs` 后，运行轨迹保存在 `outputs/traces/*.jsonl`，报告保存在 `outputs/reports/db_pool_exhausted_multi.json`。

PowerShell 查看最新 Trace：

```powershell
$trace = Get-ChildItem outputs\traces\*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content $trace.FullName
```

读 Trace 时请回答四个问题：

1. 哪个 actor 选择了哪个工具？
2. 它做选择时已经观察到了什么？
3. 工具结果如何改变了下一步？
4. 最终证据在哪一层被删除或保留？

## 4. 推荐的逐文件阅读顺序

不要从 UI 开始，也不要先钻进 OpenAI SDK。按下面顺序，理解速度最快。

### 第一遍：只建立领域对象和主链

1. `logrca/models.py`
   - 先看 `LogEvent`、`Incident`、`EvidenceRef`、`DiagnosisReport`。
   - 学会区分原始事实、假设、证据和最终报告。

2. `logrca/synthetic.py`
   - 看 `build_scenario()` 怎样构造 6 个场景。
   - 特别看 `truth` 为什么与 `incident.events` 分开。

3. `logrca/detector.py`
   - 手算 `db_pool_exhausted` 和 `normal` 的分数。
   - 理解门控为何放在 Agent 之前。

4. `logrca/store.py`
   - 看工具能返回什么、不能返回什么。
   - 注意 `limit`、`line_id` 和 `sanitize_log_text()`。

5. `logrca/runtime.py`
   - 重点看 `ToolAction`、`FinalAction`、`AgentState`、`Policy`、`AgentRuntime.run()`。
   - 这是最通用、最值得背熟的 Agent 骨架。

6. `logrca/agents.py`
   - 先看三个 instructions，再看两个 `decide()`，最后看 `IncidentManager.diagnose()`。
   - 拿纸画出 single 和 multi 两条路径。

完成第一遍后，你应该能不看源码说出：一次异常怎样从门控走到最终证据链。

### 第二遍：理解数据工程、可靠性和评测

7. `logrca/security.py`
   - 区分脱敏、Prompt injection 标记、工具权限和最终 grounding。

8. `logrca/normalizer.py`
   - `load_loghub_csv()` 读取的是 Loghub 已经结构化的 CSV，不是完整原始日志解析器。
   - `compression_stats()` 比较原文、逐行 JSON 与“模板字典 + EventId 序列”。

   接着读 `logrca/spring_parser.py`：它针对文档明确的 demo Logback layout 合并 Java 堆栈续行，展示原始文本到 `LogEvent` 的路径；不要把一个正则说成兼容所有企业日志格式。

9. `logrca/correlation.py`
   - `build_incidents()` 先按 trace/request/block ID 精确聚合，无 ID 时按时间窗退化。
   - 它是确定性预处理，不需要浪费 LLM Token。

10. `logrca/evaluation.py`
    - 看 `_prf()`、`run_benchmark()`、`compare_modes()`。
    - 理解根因准确率与证据准确率为什么是两个指标。

11. `tests/`
    - 测试是最精简的“系统行为说明书”。
    - 特别读步数上限、证据 grounding、single/multi 对比和标签隔离测试。

### 第三遍：看入口、UI 和真实模型适配

12. `logrca/cli.py` 与 `logrca/__main__.py`
    - 看命令如何映射到 Manager、Benchmark 和 Loghub 适配器。

13. `streamlit_app.py`
    - UI 只消费稳定的 `DiagnosisReport`，不应该承载核心推理规则。

14. `logrca/openai_adapter.py`
    - 最后再看。
    - 比较它怎样把同样的工具、Pydantic 输出和职责指令接到真实 SDK `Agent`。
    - 该模块通过函数内懒加载隔离可选依赖；离线代码不需要 SDK。

15. `agent_core.py`、`log_tools.py`、`run_stage1.py`
    - 这是最早的里程碑教学版，可用来对比“最小 Agent”如何演进为有数据层、安全、评测和多 Agent 的 Demo。

## 5. single Agent 与 multi Agent 到底怎么选

### 5.1 本项目的差异

single 模式：

```text
异常门控 -> 假设 Agent -> 代码 Grounding -> 最终报告
```

multi 模式：

```text
异常门控 -> 假设 Agent -> 证据审核 Agent -> 代码 Grounding -> 最终报告
```

在当前合成场景中：

- 两种模式的根因都能正确命中。
- single 会把 direct、support 和 symptom 都放进最终证据。
- multi 多用 2 次工具调用，换来更干净的根因证据集合。

这不能证明“多 Agent 永远更强”。它只证明在本任务设计和数据上，独立审核这个角色对证据精确率有价值。

### 5.2 什么时候优先单 Agent

- 任务短、工具少、一次上下文能装下。
- 角色目标一致，不存在明显的生成—审核冲突。
- 延迟、Token 和维护成本更重要。
- 流程需要强确定性，额外 Agent 只会重复工作。

### 5.3 什么时候值得多 Agent

- 子任务需要不同职责或不同权限，例如“生成候选”与“核验证据”。
- 不同阶段可以独立评测。
- 一个长 Prompt 已经充满互相冲突的要求。
- 专家只需看到局部上下文，可以降低单次上下文长度。
- 高风险结论需要第二视角和代码校验。

### 5.4 多 Agent 的代价

- 更多模型调用与 Token。
- 更高延迟。
- Agent 间传递可能丢失信息。
- 错误会级联，调试面扩大。
- 如果多个 Agent 看同一批日志、做同一件事，只是“角色扮演式复杂化”。

面试回答模板：

> 我没有为了简历盲目堆 Agent。主流程由一个 Manager 负责，默认根因专家完成检索和候选生成；只有 multi 模式才调用职责很窄的 Evidence Auditor。因为证据审核有独立输入、独立工具、独立输出和可量化指标，所以拆分是有依据的。正常请求又通过 anomaly gate 直接跳过所有 Agent。

## 6. Token 优化：本项目已经做了什么

Token 优化不是只改一句“请简洁回答”。应该从输入、检索、状态、输出和模型路由一起做。

### 6.1 已实现的优化

1. **异常门控**
   - `normal` 场景零工具调用，也不进入模型路径。

2. **先结构化，再推理**
   - `LogEvent` 统一字段，Agent 不需要反复理解各日志格式。

3. **先聚合 Incident**
   - `build_incidents()` 按关联 ID 或时间窗切成有界问题。

4. **先摘要，再按需取证**
   - 第一步只看统计，不直接搬运全部日志。

5. **只搜索异常级别**
   - `search_logs(levels=[ERROR, CRITICAL, WARN])` 把 INFO 排除。

6. **有界工具输出**
   - `limit` 和 `max_events` 防止上下文无限增大。

7. **模板化压缩**
   - `compression_stats()` 演示“模板字典 + EventId 序列”，避免重复发送相同文本。

8. **Agent 间传 ID 和 Schema**
   - 通过 `line_id` 引用证据，需要时再取原文。

9. **Runbook 检索代替临场长篇生成**
   - 输出更短，也更可信。

10. **Audit 只接收候选证据**
    - 审核员不重新扫描所有日志。

11. **Trace 只保存结果预览**
    - `tool_result` 截断到 1200 字符，避免观测文件过度膨胀。

### 6.2 生产化还可以做、但当前 Demo 没有实现的优化

以下是合理扩展方向，不要在面试中说成已经完成：

- 对重复 Incident 做摘要缓存和工具结果缓存。
- 使用向量检索或 BM25 召回历史相似事故。
- 用小模型做分类/摘要，只在不确定场景升级到强模型。
- 动态 Token budget：置信度高时早停，候选冲突时追加检索。
- 只向审核员传模板、时间与 ID，按需展开原文。
- 批量处理相同模板，避免逐行模型调用。
- 通过离线集评估不同上下文窗口和召回 Top-K。

## 7. 可操作的源码实验

不要只阅读。每个实验都应先预测，再运行，再解释 Trace 或测试结果。

### 实验 1：对比 single 和 multi

```powershell
python -X utf8 -m logrca demo --scenario db_pool_exhausted --mode single --output-dir outputs
python -X utf8 -m logrca demo --scenario db_pool_exhausted --mode multi --output-dir outputs
```

观察：

- single 应有 4 次工具调用，最终证据包含症状。
- multi 应有 6 次工具调用，最终证据只保留 direct/support。
- 两者根因相同，成本和证据精确度不同。

### 实验 2：验证正常流量不启动 Agent

```powershell
python -X utf8 -m logrca demo --scenario normal --mode multi --output-dir outputs
```

预期：`is_anomaly=false`、`tool_calls=0`、无根因代码。

### 实验 3：跑完整评测，不凭肉眼讲效果

```powershell
python -X utf8 -m logrca benchmark --mode both --output outputs\benchmark.json
```

重点比较：

- `root_cause_accuracy`
- `evidence_precision`
- `evidence_recall`
- `average_tool_calls`
- `average_latency_ms`

解释时必须带上 `scope_note`：这是合成场景功能回归，而且离线规则与场景模板共享故障目录。

### 实验 4：观察 Agent 的每一步

先运行一次 multi，再打开最新 JSONL Trace。手工标记：

- 假设 Agent 的第 1～5 次决策。
- 其中哪 4 次是工具调用。
- 审核 Agent 的两次工具调用。
- 最后的 `report_grounded`。

然后尝试仅根据 Trace 重建最终结论。如果做不到，说明 Trace 的字段还不够；这是一次 Observability 设计练习。

### 实验 5：故意制造无限循环，理解 max_steps

阅读 `tests/test_runtime_and_tools.py::test_runtime_has_hard_step_limit`。`NeverFinishPolicy` 每次都调用 `ping`，从不返回 `FinalAction`。运行：

```powershell
python -X utf8 -m pytest tests\test_runtime_and_tools.py -q
```

预期 Runtime 在 2 步后抛出明确异常，而不是永远运行。

### 实验 6：验证日志不能成为指令

阅读并运行 `test_log_security_baseline_redacts_secrets_and_flags_instructions`。输入同时含密码和“ignore previous instructions”，结果应：

- 隐藏密码值。
- 增加 `secret_redacted`。
- 增加 `instruction_like_content`。

注意这只是检测和脱敏。真正安全还依赖工具权限、只读操作和不把日志文字当控制消息。

### 实验 7：理解未知根因的降级路径

复制一个合成场景，仅把直接日志消息改成 `UNSEEN_FAILURE`，不要向 `diagnostic_signatures.py::CAUSE_SIGNATURES` 增加匹配模式，再运行 Manager。观察：

- 异常门控是否仍触发。
- 假设是否变成 `unknown`。
- 置信度与 limitations 如何变化。
- Runbook 是否返回保守的默认建议。

这个实验比再加一个已知规则更能体现“证据不足时不装懂”。

### 实验 8：调节异常阈值

用 `IncidentManager(detector=AnomalyDetector(threshold=...))` 分别设置 `0.2`、`0.8` 和 `1.0`，运行所有场景。

思考：阈值变化如何影响 FP、FN 和模型调用成本？生产上为什么不能只追求 Recall？

### 实验 9：查看 Loghub 表示压缩

选择仓库中的一个 `*_structured.csv`：

```powershell
python -X utf8 -m logrca loghub --path ..\BGL\BGL_2k.log_structured.csv
```

解释：

- `normalized_json_characters` 可能比原文更大，因为 JSON 重复字段名。
- `compact_characters` 使用模板字典和 ID 序列，才体现去重价值。
- 这里统计的是字符表示，不是 gzip 压缩率，也不是模型精确 Token 数。

### 实验 10：跑所有自动化测试

```powershell
python -X utf8 -m pytest -q
```

测试通过只能说明已覆盖行为没有回归，不能证明生产泛化、安全完备或高并发性能。

## 8. 面试高频问题与回答要点

### Q1：为什么这里需要 Agent，而不是一个 Prompt？

因为根因诊断不是一次文本生成：系统要先判断异常，再按需查询摘要、异常日志、时间线和 Runbook，观察结果后决定下一步，最后核验证据。工具调用顺序和停止时机依赖中间结果，适合 Agent 循环。确定性的预处理和安全校验仍保留在普通代码中。

### Q2：你这个 OfflinePolicy 算大模型吗？

不算。它是确定性策略，用来零成本验证 Runtime、工具契约、状态流、Trace 和 Eval。真实模型适配在 `openai_adapter.py`，实现时继续复用领域工具与 Pydantic 输出。

### Q3：为什么不用一个 Agent 完成全部事情？

默认 single 已经能完成。multi 只增加一个职责收窄的证据审核员，用额外调用换取更高证据精确度。是否启用应由任务风险、成本和评测结果决定，不是越多越好。

### Q4：如何避免把网关 503 当根因？

先按时间排序；用模板目录区分 direct/support/symptom；独立审核员再检查引用与时间；最终只把审核接受且存在于当前事故的 line ID 写入报告。

### Q5：如何防止模型伪造证据？

结构化输出要求 `line_id`；Auditor 用工具反查；Manager 最后用合法 ID 集合再过滤。Schema 负责格式，grounding 负责事实引用。

### Q6：如何减少 Token？

异常门控、Incident 切片、模板去重、先摘要后取证、只检索异常级别、限制 Top-K、Agent 间传 ID、Runbook 检索、正常场景不调用模型。进一步可做缓存、模型路由和动态预算。

### Q7：如何评测？

分层评测：异常检测看 P/R/F1，RCA 看根因准确率，解释看证据 P/R，工程成本看工具调用和延迟；再通过 Trace 做失败归因。合成数据只用于功能回归，公开 BGL 只有行级标签，不能假装有根因真值。

### Q8：如何处理 Prompt Injection？

日志一律视为不可信数据；指令和日志走不同数据通道；工具最小权限；日志文本先脱敏和标记；模型不能执行 Shell 或自动修复；输出还要代码 grounding。单靠一句 Prompt 防注入不够。

### Q9：Agent 的 Memory 在哪里？

本 Demo 只有单次运行的短期状态 `AgentState.observations`。没有长期记忆。Trace 是审计记录，不等同于记忆。

### Q10：为什么 Manager 不也是离线 Agent？

默认路径中 Manager 是确定性编排器，负责强约束流程和最终报告；这样更可控、更容易测试。可选 SDK 路径仍由 Python Manager 掌控顺序，但把 Hypothesis 与 Auditor 两个决策器替换成真实 Agent，并以代码交集强制落实审核结果。

继续看 `compose_audited_report()`：它还会从脱敏 Store 重建 evidence summary；如果审核后没有证据，将 confidence 压到不高于 0.35，只有 support、没有 direct 时压到不高于 0.55。这说明“调用 Auditor”不等于“落实 Auditor”，安全/质量政策最终仍要由运行时代码执行。

### Q11：置信度 0.93 是概率吗？

不是，是当前规则给出的启发式分数，没有做概率校准。UI 明确标注这一点。生产中需要用独立验证集做校准或改成离散置信等级。

### Q12：怎样扩展到生产？

需要真实采集链路、租户隔离、日志解析器、Trace/Metric/变更事件融合、流式 Incident 构建、模型与 Prompt 版本管理、人工审批、真实标注评测、监控和回滚。当前 Demo 已提供边界接口，但没有假装这些都已经实现。

## 9. 自测题

先遮住答案，能用自己的话讲出来才算学会。

### 基础题

1. `ToolAction` 和 `FinalAction` 的区别是什么？
2. 为什么工具结果要写入 `AgentState.observations`？
3. `Policy` 协议带来了什么解耦？
4. 为什么 `max_steps` 同时是可靠性和成本控制？
5. Instructions、Tools、Guardrails 三者各解决什么问题？
6. 为什么 `DiagnosisReport` 需要 Pydantic，而不能只输出 Markdown？
7. Trace 和 Memory 有什么不同？
8. 为什么正常日志应在 Agent 之前被门控？

### 项目理解题

9. `db_pool_exhausted` multi 模式为什么是 6 次工具调用？
10. `EVT-002-005` 是真实错误，为什么仍被 Auditor 从根因证据中剔除？
11. 假设 Agent 为什么先调用 summary，而不是直接拿全部事件？
12. `search_logs()` 有哪几个边界条件？
13. 如果 Agent 输出不存在的 `line_id`，哪两层会拦截？
14. 为什么 `truth` 不能放进 `Incident.metadata` 再交给 Agent？
15. BGL 的 `Label` 为什么不能直接评测 RCA？
16. `compression_stats()` 为什么不是 gzip 压缩评测？
17. 离线 instructions 是否真的控制了 `OfflineHypothesisPolicy`？为什么？
18. `MANAGER_INSTRUCTIONS` 在默认离线路径与在线路径中的作用有什么不同？

### 设计题

19. 如果一天有一千万行日志，你会在哪些阶段削减数据？
20. 如果 direct 模板从未见过，但多个指标异常，你如何设计 unknown 路径？
21. 如果 Auditor 和 Hypothesis Agent 结论冲突，最终决策权应放在哪里？
22. 如何证明新 Prompt 比旧 Prompt 更好，而不是“看起来更聪明”？
23. 如果加入自动修复工具，需要增加哪些授权、审批和回滚机制？
24. 哪些条件下你会删除 Auditor，回到 single Agent？

### 参考答案要点

1. 前者请求执行工具，后者携带最终结构化结果并终止循环。
2. 否则下一次决策不知道之前查到了什么，系统就不是闭环。
3. Runtime 不关心决策来自规则还是 LLM。
4. 它避免无限循环，也封顶调用次数、延迟和费用。
5. Instructions 定目标，Tools 给能力，Guardrails 限边界；三者不能互相替代。
6. Schema 可验证、可测试、可供 CLI/UI/Eval 稳定消费。
7. Memory 影响未来决策；Trace 用于记录、调试和审计。
8. 正常流量占多数时，可以显著降低成本和误诊。
9. 假设专家 4 次，审核专家 2 次；两次 FinalAction 都不算工具调用。
10. 它是根因造成的上游可见症状，不能单独证明数据库连接池耗尽。
11. 先了解范围，再按需取证，减少上下文并帮助制定检索策略。
12. 当前事故范围、查询/等级/服务过滤、1～100 条限制、返回脱敏摘要。
13. Auditor 的 `get_events_by_ids()` 和 Manager 的最终合法 ID 过滤。
14. 会造成标签泄漏，让评测结果失真。
15. 它表示行是否异常，没有事故级根因标签和证据链标签。
16. 它比较三种字符表示，目的是说明模板去重，不测压缩算法。
17. 不会；离线行为写死在 `decide()`，instructions 只作契约和 Trace 数据。
18. 默认 Manager 是 Python 编排器；在线适配器会把 Manager 指令交给真实模型。
19. 采集过滤、模板化、Incident 聚合、异常门控、摘要、Top-K 检索、按需原文展开、缓存。
20. 返回 unknown 和低置信度，补查指标/Trace/变更记录，不强行映射已知根因。
21. 由 Manager 依据明确政策处理，必要时转人工；专家输出只是输入。
22. 固定评测集、版本化配置，比较任务指标、证据质量、延迟、成本和失败类型。
23. 最小权限、参数白名单、dry-run、人工批准、幂等、超时、回滚、审计和熔断。
24. 审核没有稳定增益、延迟/成本不合算，或任务风险低且代码 grounding 已足够时。

## 10. 一周后回来，最短复习路线

如果隔了一段时间，按以下顺序恢复记忆：

1. 跑 `db_pool_exhausted` 的 multi 模式。
2. 看最新 Trace，口述 6 次工具调用。
3. 重读 `runtime.py` 的 `AgentRuntime.run()`。
4. 重读 `agents.py` 的两个 `decide()` 和 `IncidentManager.diagnose()`。
5. 跑 `benchmark --mode both`，解释 single/multi 取舍。
6. 跑 pytest，说明每类测试保护什么边界。
7. 最后再看 `openai_adapter.py`，讲清哪些部分替换了，哪些领域层保持不变。

你真正需要学会的不是记住每行代码，而是能回答：

> 为什么这个步骤应该由确定性代码完成，为什么那个步骤需要 Agent 决策；Agent 能看到什么、能调用什么、何时停止；结论如何绑定到真实证据；改动后怎样用 Trace 和 Eval 证明系统变好了。

能把这五件事结合 `db_pool_exhausted` 的源码和运行结果讲清楚，你就不只是“用过 Agent”，而是已经理解了一个小型 Agent 系统的设计、实现与验证方法。
