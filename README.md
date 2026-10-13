# LogRCA · Spring 日志辅助排查工具

本地上传一份 Spring 日志，查看根因假设，按引用ID核对源日志行号与堆栈，并下载报告、证据和Trace。支持离线规则基线和 DeepSeek 工具调用；真实模型效果需要实际运行验证。

**新用户先看 [下载后的运行与验收步骤](QUICKSTART.md)。** 独立项目不需要下载整个Loghub仓库。网页启动：

```powershell
python -m pip install -r requirements.txt
python -X utf8 run_web.py
```

浏览器打开 `http://127.0.0.1:8501`，先用内置示例完成一次离线诊断，再选择 DeepSeek 或上传自己的日志。配置密钥后需要在同一终端重新启动网页。源码、测试和说明可用 `python scripts/package_project.py` 打成独立ZIP；数据归属见 [DATA_SOURCES](docs/DATA_SOURCES.md)。

这是一个面向学习与求职展示的智能日志诊断原型：它从多格式日志得到统一事件，先用低成本规则做异常门控，再由诊断 Agent 检索证据、形成根因假设；多 Agent 模式会增加一个独立 Evidence Auditor，剔除把级联症状误当根因证据的引用，最终输出结构化报告、排查建议和完整 Trace。

项目目标是做出一个**可运行、可测试、可评测、可诚实讲清楚**的 Demo，而不是伪装成已经在生产环境验证的平台。

当前版本有意**不使用 RAG、向量数据库或知识图谱**。本地语义签名和按 code 精确读取的 Runbook 是可审查基线，不是检索增强生成；先把 Agent loop、工具边界、证据 Grounding、拒答和评测做扎实。

## 五分钟运行

推荐 Python 3.11 或 3.12。在当前目录运行：

```powershell
pip install -r requirements.txt

# 完整多 Agent 诊断
python -X utf8 -m logrca demo --scenario db_pool_exhausted --mode multi

# 从原始 Spring/Logback 文本解析（含 Java 堆栈续行）并诊断
python -X utf8 -m logrca spring --path data\spring_db_pool.log --mode multi

# 任意本地 Spring/Logback 或 Loghub structured CSV：自动识别并完成全链路
python -X utf8 -m logrca analyze --path data\spring_db_pool.log --mode multi --output-dir outputs\analyze

# 单 Agent / 多 Agent 对照评测
python -X utf8 -m logrca benchmark --mode both

# 读取仓库内真实 Loghub BGL 样本，计算模板表示体积和行级基线
python -X utf8 -m logrca loghub --path ..\BGL\BGL_2k.log_structured.csv --bgl-eval

# P0：120 个标签侧车案例，混入 16 个 Loghub 数据集的真实消息噪声
python -X utf8 -m logrca p0-benchmark --split test --mode both --loghub-root .. --output-dir outputs_p0

# 审计 16 个 Loghub structured CSV 的接入字段与时间解析覆盖
python -X utf8 -m logrca audit-loghub --root .. --limit 2000 --output outputs_p0\ingestion_audit.json

# 测试
python -X utf8 -m pytest

# 可视化页面
streamlit run streamlit_app.py
```

可用合成场景：`normal`、`db_pool_exhausted`、`inventory_timeout`、`redis_unavailable`、`payment_auth_failure`、`disk_full`。

输出写入 `outputs/`、`outputs_p0/` 或命令指定的位置。Trace 是动作、工具调用和结果摘要，不是模型私有思维链。

## DeepSeek：让模型真正参与 Spring 日志排查

新增路径使用 `deepseek-flash`，通过 Python 自带的 `urllib` 请求 DeepSeek，不需要安装 OpenAI SDK。原来的不带 `--backend deepseek` 的命令仍是离线规则基线。连接检查脚本只问一个普通问题；下面的入口会让模型实际选择工具、读取日志、提交候选，再由另一次模型对话审核证据。

在项目目录中，使用**已经设置新密钥的同一个 PowerShell 窗口**运行：

```powershell
python -X utf8 examples\deepseek_agent_demo.py
```

如果新开了窗口，先在本地设置密钥，输入时显示星号，不需要把密钥发给任何人：

```powershell
$deepseekKey = Read-Host '请输入 DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $deepseekKey).Password
python -X utf8 examples\deepseek_agent_demo.py
```

在项目目录中，等价的完整命令是：

```powershell
python -X utf8 -m logrca analyze --path data\spring_db_pool.log --format spring --backend deepseek --model deepseek-flash --mode multi --max-incidents 1 --output-dir outputs\deepseek_spring
```

完成后查看三个地方：

- `outputs/deepseek_spring/reports/`：最终根因、证据行、建议和限制。
- `outputs/deepseek_spring/traces/`：逐行 JSON 记录的模型请求、真实工具调用、完整的有界工具结果、候选、审核和最终引用校验。失败也会留下 Trace。
- 同一输出目录下的 `run-*.json`：实际模型名称、请求次数、Token、耗时、状态和文件路径。`returned_models` 来自服务端；没有提供用量时会计入 `usage_missing_requests`，不能把缺失当作真正零消耗。

其中 `requests` 统计调用尝试，`completed_requests` 统计收到响应的次数。没有密钥或请求失败的尝试也会留下记录；它们不代表模型已完成回答。

样例中预期关注的是“连接获取超时/连接池无可用连接 → 数据库操作失败 → 网关503”。日志无法单独证明更深原因是连接泄漏还是慢SQL，因此这些只能列为下一步检查。模型结果可能不同；报告里的 `confidence` 是受约束的评分，不是经过统计校准的成功概率。

### 你需要看懂的运行过程

DeepSeek 路径的门控会让单条 `ERROR` 也进入排查，避免只记录一次网关503的事故被跳过。`is_anomaly=true` 在这里表示进入异常排查流程，并不表示已确定生产故障原因。只有正常 INFO 的案例仍然不调用模型。历史离线基线保留原阈值，因此两种路径的门控统计也应分别报告。

```text
Spring 日志 → 解析为 LogEvent → 按 trace_id 分组为 Incident
    → 规则异常门控（正常日志直接结束，0次模型调用）
    → Python Manager 启动根因专家
         DeepSeek 请求 search_logs/get_timeline 等工具
         Python 校验工具名、参数、事故范围 → 执行本地只读函数
         把结果连同 tool_call_id 交回 DeepSeek → 模型输出候选 JSON
    → Python Manager 启动证据审核专家（同一模型，另一段对话）
         再读取候选引用的日志 → 输出审核 JSON
    → 代码校验“真实存在 ∩ 专家已读 ∩ 候选引用 ∩ 审核接受”
    → 没有合格直接证据则 unknown → 保存报告、Trace 和实际用量
```

这里的大模型负责“选择下一次查询、解释日志、形成假设、判断引用是否支持假设”。Python 负责“调用接口、执行工具、管理状态、检查结构和引用、控制预算”。工具是现成的本地 Python 函数，目前没有 MCP，也没有长期记忆；这些不是跑通此闭环的必要条件。

例如，模型返回 `search_logs({"levels":["ERROR"],"limit":20})`，程序查出当前事故的日志，再发送形如 `{"role":"tool","tool_call_id":"模型给的调用ID","content":"日志JSON"}` 的消息。模型本身不读取你的磁盘，也不会执行修复。一个响应包含多个工具时，程序先把所有结果配上各自的 ID，再请求模型下一轮。这遵循 [DeepSeek 工具调用协议](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/)。

最终输出采用 [JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/) 并在本地用 Pydantic 验证；JSON 格式合法不代表结论正确。一次输出校验失败最多修正一次，仍失败就明确报错。证据审核会增加模型调用和上下文成本，不能宣称它保证消除幻觉，也不能把旧的离线评测成绩当作 DeepSeek 的成绩。

默认限制针对**每一个事故**：两位专家合计最多12次模型请求、每位最多6次、每次最多4个工具、单工具最多30行且返回约12000字符，单次输出最多1800 Token。请求网络超时30秒，事故后续请求受180秒预算约束；已报告总用量达到20000 Token 时停止追加调用。最后一次请求的输入消耗可能使总 Token 超过阈值，所以这不是精确的预付费硬上限。没有自动重试，也不会在 API 失败时悄悄切换成离线结果。CLI 的 DeepSeek 路径默认只分析1个事故；增加范围需要显式传 `--max-incidents`。

### 准备面试时怎么学这部分

不需要通读新增文件。先真正运行一次，再打开本次 Trace，对照下面四个入口各看一小段：

| 要回答的问题 | 对应入口 | 要理解的细节 |
|---|---|---|
| 接的哪个模型，如何请求？ | `logrca/deepseek_adapter.py` 的 `DeepSeekTransport.complete` | 地址、模型ID、密钥、非思考模式、超时 |
| 模型怎样用工具？ | `DeepSeekPolicy.decide` | tools 描述、tool_calls、Python执行、tool_call_id回传、多轮消息 |
| 谁控制流程，如何防止无限调用？ | `DeepSeekIncidentManager.diagnose`、`RequestSession.request` | 规则门控、两位专家顺序、次数与用量限制 |
| 如何减少编造？ | Manager 中的证据过滤与 `compose_audited_report` | 已读引用、候选与审核交集、源日志替换摘要、证据不足返回unknown |

能用自己的话解释这四行，并能指着 Trace 举例，比逐行背代码更适合这一周的准备。但“完全不看任何源码”会让你很难回答为什么拒绝某个工具参数、引用如何被删除这样的具体追问。

### 五个固定学习案例

`data/spring_agent_cases.log` 包含连接池获取超时、Redis连接拒绝、只有网关503、未知严重异常、正常请求五组**构造日志**。它们用于学习和小样本验证，不是生产环境的日志，也不能据此证明部署效果。前两组预期能提出对应根因，接下来两组应保留未知，最后一组应不调用模型。

先跑通单个数据库案例，再在项目目录中显式扩大范围：

```powershell
python -X utf8 -m logrca analyze --path data\spring_agent_cases.log --backend deepseek --max-incidents 5 --output-dir outputs\deepseek_cases
```

此命令可能产生多轮付费调用。要评价模型是否稳定，需要同一批日志重复运行并核对根因、有效引用、unknown与失败情况；自动化测试中的模拟响应只验证程序逻辑，不衡量真实模型准确率。当前解析器支持这里展示的 `时间 级别 [service] [trace=ID] 事件ID - 消息` 布局及 Java 堆栈续行，未承诺兼容所有 Spring 默认日志格式。

## 架构一眼看懂

```text
Loghub CSV / Spring-Logback 文本 / 合成 Spring 事件
              │
              ▼
        Normalizer → LogEvent
              │
              ▼
 Correlation ID / 时间窗 → Incident
              │
              ▼
      Anomaly Gate（便宜、确定性）
          │正常                 │异常
          └── 直接结束          ▼
                         Incident Manager
                           │
                           ├── Hypothesis Agent
                           │      └── 有界只读工具
                           │
                           └── Evidence Auditor（multi）
                                      │
                                      ▼
                         Grounding Guardrail
                                      │
                                      ▼
                         DiagnosisReport + Trace + Eval
```

为什么是混合架构：解析、过滤、计数、限流、引用校验都有确定答案，交给普通代码更便宜可靠；选择下一条证据、区分根因与症状、解释跨服务关系更需要 Agent 式决策。生产系统不应该让 LLM 逐行读取几万条日志。

## 当前到底实现了什么

| 能力 | 实现位置 | 状态 |
|---|---|---|
| 统一日志/事故/证据/报告 Schema | `logrca/models.py` | 已实现、Pydantic 校验 |
| 六个 Spring 风格场景及隐藏真值 | `logrca/synthetic.py` | 已实现、确定性 |
| 120 案例标签侧车评测集（8 已知故障 + normal + OOD） | `logrca/blind_data.py` | 已实现；精确证据文案/模板按 split 隔离 |
| Loghub structured CSV 适配 | `logrca/normalizer.py` | 已实现，BGL/HDFS/OpenStack 已测 |
| 16 个 Loghub 数据集接入审计 | `logrca/ingestion_audit.py` | 已实现；32,000 行审计产物已生成 |
| Spring/Logback 文本与多行堆栈解析 | `logrca/spring_parser.py` | 常见 Boot 文本及指定 Logback 布局；不支持任意自定义布局 |
| Spring ECS/Logstash JSONL | `logrca/spring_json_parser.py` | 已实现字段、时区和类型校验 |
| 网页上传、原始行号核对、结果下载 | `streamlit_app.py` / `logrca/file_app.py` | 已接共享文件管线；模型请求需显式点击 |
| 模板字典 + EventId 序列体积实验 | `logrca/normalizer.py` | 已实现 |
| trace/request/block ID 关联及时间窗兜底 | `logrca/correlation.py` | 已实现基线 |
| 异常门控 | `logrca/detector.py` | 已实现规则基线 |
| 有界只读日志工具与 Runbook | `logrca/store.py` | 已实现 |
| 9 类本地语义根因签名与未知类拒答 | `logrca/diagnostic_signatures.py` | 已实现；不是 LLM/RAG |
| 通用 Agent loop、状态、Tool Registry、Trace | `logrca/runtime.py` | 已实现 |
| 单 Agent 根因假设 | `logrca/agents.py` | 已实现离线策略 |
| Manager + Hypothesis + Evidence Auditor | `logrca/agents.py` | 已实现离线多 Agent |
| 引用存在性 Grounding | `IncidentManager.diagnose` | 已实现代码级校验 |
| 敏感字段脱敏/指令型日志标记 | `logrca/security.py` | 已实现基础版 |
| Detection / Top-K RCA / MRR / Evidence / Grounding / 成本指标 | `logrca/benchmarking.py` | 已实现，含 Bootstrap 与分层结果 |
| 扰动与反事实变换 | `logrca/robustness.py` | 已实现 ID 置换、删证据、干扰、乱序、重复、截断 |
| 标签隔离 P0 runner 与 Single/Multi 消融 | `logrca/p0_runner.py` | 已实现，失败样本不从分母删除 |
| 本地文件到报告的完整批处理 | `logrca/pipeline.py` | 已实现 Spring/Logback 与 Loghub CSV |
| CLI、Streamlit、JSONL Trace | `cli.py` / `streamlit_app.py` | 已实现 |
| OpenAI Agents SDK 真实模型路径 | `logrca/openai_adapter.py` | 可选；需 Key，未纳入离线 CI |
| 重复真实模型评测、Token/成本/稳定性记录 | `logrca/live_benchmark.py` | harness 已测；当前未运行真实 API 实验 |
| Kafka/Flink/ES/OTel/生产权限 | `docs/PRODUCTION.md` | 仅设计，未实现 |

旧的 `run_stage1.py`、`agent_core.py` 和 `log_tools.py` 被保留为“最小 Agent loop”第一课；完整版本使用 `logrca/` 包。

## 为什么默认 Agent 是离线规则

`OfflineHypothesisPolicy` 和 `OfflineEvidenceAuditPolicy` 是可复现的教学替身，不是 LLM。它们仍然真实走完：

```text
读取 State → 选择 ToolAction → Runtime 执行 → Observation 回填
→ 根据新状态继续选择 → FinalAction（结构化输出）
```

这样做有三个目的：

1. 不依赖 API Key，项目任何时候都能演示。
2. Agent loop、工具权限、最大步数、Trace、Schema、评测都能做单元测试。
3. 把“编排是否正确”和“模型是否聪明”分开定位。

这也是必须在面试中主动说明的边界：离线策略在合成模板上取得高分，只能证明工程链路正确，不能证明它能泛化到未知生产故障。

## 实际评测结果

六场景 smoke regression 仍用于快速检查链路：Single/Multi 的异常 F1 和根因 Top-1 都为 `1.0000`；Evidence Precision 从 `0.5882` 提升到 `1.0000`，平均工具调用从 `3.333` 增加到 `5.000`。它只是功能回归，不是泛化评测。

更严格的 P0 结果来自 `outputs_p0/result.json`：120 个开发者合成案例中的 40 个 test 案例，包含 8 类已知故障、正常类和 OOD；使用 seed `20260921`，并从 16 个 Loghub 数据集各抽取 50 行作为背景池。当前离线确定性策略结果为：

| Test 指标（40 cases） | Single | Multi |
|---|---:|---:|
| Incident Detection F1 | 1.0000 | 1.0000 |
| RCA Top-1（含已删除 direct 证据的历史根因） | 0.8125 | 0.8125 |
| RCA Top-1（direct 证据仍可见，26 cases） | 1.0000 | 1.0000 |
| OOD unknown abstention | 1.0000 | 1.0000 |
| 删除 direct 证据后的 abstention（6 cases） | 1.0000 | 1.0000 |
| Evidence micro Precision | 0.4103 | 0.9275 |
| Evidence micro Recall | 1.0000 | 1.0000 |
| Evidence micro F1 | 0.5818 | 0.9624 |
| Evidence exact-set accuracy | 0.1000 | 0.8750 |
| Invalid citation rate | 0.0000 | 0.0000 |
| 平均工具调用 | 3.600 | 5.400 |

这里整体 RCA `0.8125` 不能脱离反事实指标解读：6 个已知故障案例主动删除了直接根因证据，系统全部选择弃权；它们在普通 Top-1 中仍会被算错，却是更安全的行为。另一个独立 trace-corruption 子评测会把事件重新送入 `correlation.py`，而不是只修改字段；异常案例会强制移动 direct 或 support 证据，并按证据角色分层统计：test 中事件守恒率 `1.0000`，已知历史根因恢复率 `0.7500`；被移动 direct 的已知故障为 4/4 恢复，被移动 support 的为 2/4，两个失败分别暴露噪声误导和 direct 证据缺失。

多 Agent 的收益也很具体：Auditor 大幅删除“确实发生、但不能证明根因”的日志，Evidence Precision 从 `0.4103` 提升到 `0.9275`；代价是平均工具调用从 `3.6` 增到 `5.4`。它没有提高当前离线候选生成器的 RCA Top-1，所以不能说“多 Agent 在所有维度更强”。

这些数值来自同仓开发者设计的语义故障目录。虽然真值字段、ID/服务名/时间规律和跨 split 的精确证据文案已隔离，离线正则也不读取 sidecar，但规则与挑战集仍在同一个开发过程中演进。它能证明工程链路和已定义语义边界，不能证明面对独立生产分布仍有相同准确率。最终数字始终以结果文件中的 manifest hash 和 run config 为准。

本地 BGL 2k 样本的 `Level in {ERROR, FATAL, SEVERE}` 行级基线：

```text
TP=143, FP=252, FN=0, TN=1605
Precision=0.3620, Recall=1.0000, F1=0.5316
```

它说明“高日志级别不等于真正异常”。BGL 的 `Label` 只是行级异常标签，没有完整根因和证据链，因此绝不能用它声称 RCA 准确率。

模板表示体积实验（字符数，不冒充精确 Token）：

| 数据 | 事件数 | 模板数 | 模板字典+ID序列 / 原始消息 |
|---|---:|---:|---:|
| BGL | 2000 | 120 | 23.04% |
| HDFS | 2000 | 14 | 7.25% |
| OpenStack | 2000 | 43 | 7.57% |

逐行结构化 JSON 反而远大于原始消息。**JSON 解决字段统一；模板聚合、候选窗口、有界工具和按需取证才减少上下文。**

## 可选真实模型路径

```powershell
pip install -r requirements-openai.txt
$env:OPENAI_API_KEY="..."
python -X utf8 -m logrca live --scenario db_pool_exhausted --model <你明确选择的模型名>

# 在标签隔离 test 集上做可重复 single/multi 对照；价格必须由运行者显式提供
python -X utf8 -m logrca live-benchmark --model <模型名> --split test --max-cases 20 --repeats 3 --mode both --output outputs\live_benchmark.json
```

该路径使用 OpenAI Agents SDK 运行 Hypothesis Agent 与 Evidence Auditor；Python Manager 强制按“生成候选 → 独立审核 → accepted 证据取交集 → 组装 `DiagnosisReport`”执行，避免最终模型仅在 Prompt 层忽略审核结果。模型名必须显式传入，避免依赖默认模型导致成本和行为悄悄变化。`live-benchmark` 还记录重复运行稳定性、请求数、Token、延迟、结构化失败与普通失败；只有同时传入当日确认的输入/输出价格才估算成本。

当前机器没有可用 API Key，因此只完成了 SDK 适配、fake-runner 控制逻辑测试和“无 Key 明确拒绝运行”；没有声称已经获得真实模型准确率、Token、成本或延迟。默认演示和全部离线测试都不依赖它。

## 推荐源码学习顺序

1. 先运行 `python -m logrca demo`，只看报告与 Trace。
2. 读 `models.py`：理解 Agent 之间传什么，而不是先看 Prompt。
3. 读 `runtime.py`：逐行跟踪 `AgentRuntime.run`，这是 Agent 的发动机。
4. 读 `store.py`：理解模型为什么只能通过有界工具访问数据。
5. 读 `agents.py` 的两个 Policy，再读 `IncidentManager.diagnose`。
6. 对照运行 single 和 multi，看 Auditor 做了什么、付出什么成本。
7. 读 `pipeline.py`，串起真实文件解析、关联、逐 Incident 诊断与落盘。
8. 读 `blind_data.py`、`benchmarking.py`、`p0_runner.py` 和对应测试，理解标签隔离、反事实指标与失败样本保留。
9. 最后读 `openai_adapter.py` 和 `live_benchmark.py`，看真实模型如何替换离线决策器并被重复评测。

详细课程见：

- [Agent 源码学习指南](docs/LEARNING_GUIDE.md)
- [项目实施与后续学习计划](docs/PROJECT_PLAN.md)
- [系统架构](docs/ARCHITECTURE.md)
- [生产化设计](docs/PRODUCTION.md)
- [数据与评测](docs/EVALUATION.md)
- [P0 完成说明与源码入口](docs/P0_COMPLETION.md)
- [面试与简历指南](docs/INTERVIEW_GUIDE.md)

## 项目边界：简历上怎么诚实写

可以写：

> 基于 16 类公开 Loghub 样本和 120 个真值侧车独立存放的 Spring 风格事故，构建本地日志 RCA Agent：实现多格式解析、Trace/时间窗关联、异常门控、有界工具、Manager–Hypothesis–Evidence Auditor 编排、结构化证据引用及 OOD/反事实评测。40-case test 中，多 Agent 将 Evidence micro Precision 从 0.410 提升至 0.928（工具调用 3.6→5.4）；direct 证据可见子集 Top-1 为 1.000，删除 direct 证据后 6/6 安全弃权。结果来自开发者合成语义基线，不外推为生产效果。

不能写：

- “已在公司生产环境上线”——这个仓库没有公司代码或数据。
- “真实故障定位准确率 100%”——高分来自已知合成模板，是功能回归。
- “独立外部盲测 Top-1 100%”——test 隔离精确文案，但规则与语料仍在同仓开发。
- “Loghub 上根因准确率 100%”——本地公开数据没有完整 RCA 真值。
- “离线 Policy 是大模型推理”——它是确定性策略。
- “把日志转 JSON 节省 80% Token”——逐行 JSON 往往更大，真正节省来自模板化和按需取证。

项目使用的公开数据仓库：[Loghub](https://github.com/logpai/loghub) 与 [Loghub-2.0](https://github.com/logpai/Loghub-2.0)。
