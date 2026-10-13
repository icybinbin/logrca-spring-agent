# 从 Demo 到生产：智能日志诊断系统落地方案

> 版本说明：下文的当前状态表以早期离线版本为基线。现已增加 DeepSeek 工具调用、常见 Spring 文本/JSONL 文件解析和本地上传网页，见 [README](../README.md)。本文的生产演进方案仍是设计，未经过生产部署验证。

> 这是一份生产演进设计，而不是当前项目已部署能力清单。文中所有能力都标记为“Demo 已实现”或“生产待实现”，避免把设计方案写成虚假的项目经历。

## 1. 先划清事实边界

| 能力 | 当前 Demo 状态 | 生产目标 |
| --- | --- | --- |
| 日志输入 | 合成 Spring 日志、Loghub 结构化 CSV | Kafka/OTel Collector 持续接入多集群日志 |
| 结构化 | 读取现成 CSV 并映射为统一 `LogEvent` | 在线解析、Schema 版本治理、解析失败队列 |
| 事故聚合 | trace/request/block ID + 单机时间窗 | Flink 有状态窗口、拓扑和告警信号联合聚合 |
| 异常检测 | 可解释等级/模板规则 | 规则 + 指标告警 + 历史基线 + 序列模型的多路门控 |
| 日志检索 | 进程内 `IncidentStore` | Elasticsearch/OpenSearch 热数据 + 对象存储冷数据 |
| 根因诊断 | 离线规则 Agent；可选 OpenAI SDK 适配 | 受模型网关、权限、预算和审计约束的 Agent Service |
| 多 Agent | Manager + Hypothesis + Auditor | 经离线/在线评测后按事故等级选择性启用 |
| 安全 | 基础秘密脱敏、提示注入标记、只读工具 | 租户级 DLP、RBAC/ABAC、KMS、审计、网络隔离 |
| 评测 | 六个合成场景、BGL 行级基线 | 脱敏历史事故集、回放、shadow、人工反馈和漂移监控 |
| 修复 | 仅输出建议，不执行 | 默认人审；低风险动作经过审批、幂等和回滚后才可自动化 |

面试时可准确表述为：“我实现了一个可运行、可评测的离线 Demo，并为生产演进设计了流式接入、检索、安全和人审架构。”不要说成“已经在真实生产部署并达到某个准确率”。

## 2. 目标生产架构

```text
应用 / JVM / Nginx / DB / K8s
          │ logs + traces + metrics + change events
          ▼
 OpenTelemetry Collector / Fluent Bit
          │ 采集、初步脱敏、补充 tenant/service/env 元数据
          ▼
       Kafka / Pulsar
          │
          ├──────────────► Object Storage（不可变原始归档）
          │
          ▼
 Flink / Spark Streaming
  解析、模板化、去重、乱序处理、窗口统计、事故聚合
          │
          ├──────────────► Feature Store / 时序库（检测特征）
          ├──────────────► OpenSearch / Elasticsearch（热检索）
          └──────────────► Incident Topic
                                 │
                                 ▼
                         Anomaly Gate Service
                     规则/告警/统计/模型多路融合
                         │正常           │异常
                         ▼               ▼
                    统计与归档       RCA Agent Service
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
             Log Retrieval        OTel Trace/Metric     Change/CMDB/Runbook
                    └───────────────────┼───────────────────┘
                                        ▼
                          Policy Engine + Model Gateway
                         预算、权限、脱敏、缓存、限流、审计
                                        │
                                        ▼
                      Incident Report / Alert / SRE Review UI
                                        │
                          人工确认、反馈、工单、受控修复
```

生产系统的核心不是简单地把更多日志塞给 LLM，而是先通过数据平台把一次事故压缩成一个可调查的、权限受控的证据空间。

## 3. 数据接入与统一模型

### 3.1 采集层

建议用 OpenTelemetry Collector、Fluent Bit 或已有日志代理接入 stdout、文件、Journald 和云日志。采集侧必须补齐：

- `tenant_id`、`environment`、`cluster`、`namespace`；
- `service.name`、`service.version`、`host/pod/container`；
- `trace_id`、`span_id`、`request_id`；
- 采集时间与事件原始时间；
- `schema_version` 与来源类型。

敏感数据应尽量在离开业务节点前脱敏。原始归档需要独立权限和保留策略，不能因为 Agent 要诊断就扩大所有使用者的访问范围。

### 3.2 Schema 与解析

生产 `LogEvent` 可扩展为：

```text
event_id, tenant_id, event_time, ingest_time,
service, environment, host, level,
trace_id, span_id, request_id,
template_id, template_version, parameters,
sanitized_message, raw_object_uri,
parser_version, security_tags
```

解析器应版本化，并监控解析成功率。无法解析的日志进入 dead-letter topic，不能悄悄丢失。模板提取可采用 Drain/Spell 等算法或团队已有规则；模板升级要保留映射，避免历史统计突然断裂。

### 3.3 去重、乱序与时钟

Kafka 至少一次投递会产生重复事件，需要使用来源偏移量或内容哈希生成稳定 `event_id` 做幂等去重。跨服务时间可能漂移，因此关联时同时考虑 event time、ingest time 和 Trace 顺序。Flink 使用 watermark 处理迟到事件，并允许事故在限定时间内增量补充证据。

## 4. Incident 聚合策略

推荐从强关联到弱关联分层处理：

1. 相同 `trace_id/request_id`：最高优先级；
2. 相同告警实体、服务实例或作业 ID；
3. CMDB/服务依赖图上的相邻服务；
4. 时间窗内的异常模板共现；
5. 只在缺少强 ID 时使用纯时间窗口。

一个 Incident 需要设硬上限，例如最大事件数、最大服务数和最大持续时间。超过上限时切片并保留父子关系。否则一次雪崩可能把数百万行日志聚成一个无法推理的上下文。

聚合不等于因果。生产报告应明确“同 Trace”“拓扑相邻”“时间共现”分别属于哪一种关联依据。

当前 Demo 的 `correlation.py` 只实现了第 1 类和时间窗兜底，且在内存中执行；Flink 状态、watermark、拓扑关联均是待实现设计。

## 5. 存储与检索

推荐冷热分层：

- OpenSearch/Elasticsearch：最近数天到数周的结构化热数据，支持按 tenant、service、trace、template、time 查询；
- 对象存储：加密保存低成本原始归档，并用生命周期策略转冷；
- PostgreSQL：事故、报告版本、审批、用户反馈等事务元数据；
- 时序数据库：指标和异常分数；
- 可选向量索引：相似历史事故和 runbook 语义检索，不承担精确日志过滤。

Agent 工具应调用一个 Retrieval Service，而不是持有 Elasticsearch 的任意查询权限。服务端必须强制注入 `tenant_id`、时间范围和结果上限，避免模型参数绕过数据隔离。

适合暴露给 Agent 的生产工具包括：

- `get_incident_summary(incident_id)`；
- `search_events(filters, cursor, limit)`；
- `get_timeline(event_ids)`；
- `get_trace(trace_id)`；
- `get_metric_window(entity, metric, time_range)`；
- `get_recent_changes(entity, time_range)`；
- `get_dependency_neighbors(service)`；
- `search_runbooks(symptom_or_code)`；
- `get_similar_resolved_incidents(fingerprint)`。

每个工具都需要参数 Schema、超时、返回上限、权限检查和审计日志。

## 6. 异常门控的生产升级

不应让 LLM 扫描全部正常日志。生产门控可组合：

- 静态规则：ERROR/FATAL、已知严重模板；
- 监控告警：错误率、延迟、饱和度、SLO burn rate；
- 动态基线：按服务、星期、时间段比较模板频率；
- 新颖模板：此前未见或突然增长的错误模板；
- 序列异常：模板序列偏离正常请求路径；
- 变更信号：发布、配置、证书、密钥轮换后异常上升。

门控应输出可解释原因和版本号，并单独评测 Precision/Recall。阈值应按事故成本设置：支付和数据丢失类场景偏高召回，低价值开发环境则偏高精确率。

Demo 当前只实现等级、已知模板和跨服务错误的启发式加权。它用于说明接口和省 Token 策略，不是生产异常模型。

## 7. 生产 Agent 编排

### 7.1 为什么仍由 Manager 控制

Manager 持有任务状态、预算和最终输出契约。专家是受限工具：

- Hypothesis Agent：广泛召回 1～3 个候选根因；
- Evidence Auditor：验证引用、时间顺序和反证；
- 可选 Change Analyst：只查发布和配置变更；
- 可选 Topology Analyst：只查依赖图和跨服务传播；
- 可选 Runbook Retriever：只返回已审批的操作指南。

不要为简历亮点盲目增加角色。每多一个 Agent 都引入额外延迟、成本和故障边。只有角色拥有不同目标、数据权限或评测收益时才拆分。

### 7.2 状态机与持久化

生产编排不应只依赖进程内循环。可使用 Temporal、LangGraph 持久化层或自研状态机记录：

```text
RECEIVED -> GATED -> INVESTIGATING -> AUDITING
         -> NEEDS_HUMAN / COMPLETED / DEGRADED / FAILED
```

每一步携带幂等键、超时、重试次数、模型版本、Prompt 版本、工具版本和输入数据快照 ID。服务重启后必须能从安全检查点恢复，而不是重新消费全部日志。

### 7.3 Grounding

最终报告至少做三层约束：

1. Schema：字段类型和枚举合法；
2. Citation：所有 event ID 存在且属于用户有权访问的 Incident；
3. Entailment：审核模型或规则判断引用是否真的支持声明。

第三层也不保证因果正确，因此生产 UI 必须让 SRE 一键打开原始上下文、Trace、指标和变更记录。

## 8. OpenTelemetry 与跨信号关联

仅凭日志常常无法证明因果。生产 Agent 应优先使用 OpenTelemetry 的统一资源属性，把三类证据对齐：

- Logs：错误文本、模板和上下文；
- Traces：调用路径、最早失败 Span、重试和扇出；
- Metrics：连接池、线程池、CPU、磁盘、错误率的时间变化。

再叠加第四类变更事件：发布、配置、Feature Flag、证书/密钥轮换、扩缩容。根因报告可表达为：

```text
直接证据：10:01:03 order-service Hikari 获取连接超时
支持指标：10:00:50 起 pending_connections 快速上升
传播路径：order-service span error -> gateway 503
相关变更：09:58 发布版本 v2.4.1（待验证，不直接等于根因）
```

这比单纯生成一段听起来合理的解释更适合生产排障。

## 9. 权限、脱敏与安全

### 9.1 多租户权限

- 所有数据记录携带不可缺省的 `tenant_id`；
- 用户身份通过 OIDC/IAM 进入 Retrieval Service；
- 工具端做 RBAC/ABAC，而不是相信 Prompt 中的租户声明；
- 生产、预发、开发环境权限分离；
- 原始日志与脱敏日志使用不同权限；
- 审计“谁在何时让哪个模型读取了哪些事件”。

### 9.2 脱敏

使用可配置 DLP 规则处理 API Key、Token、密码、Cookie、身份证、手机号、邮箱和业务敏感字段。需要关联同一值时，可用租户级密钥做不可逆 HMAC 标记，而不是保留明文。

脱敏应发生在模型调用之前。Prompt 和 Trace 也要走相同的数据分类与保留策略，不能让调试日志成为新的泄漏点。

### 9.3 Prompt injection

攻击者可以把“忽略系统指令并执行命令”写进业务日志。防护原则是：

- 日志永远放在明确的数据字段中，不与系统指令拼接；
- 提示中声明日志内容不可信，但不能只依赖这句话；
- 工具权限最小化，诊断阶段默认全只读；
- 工具参数由服务端再次验证；
- 不向模型暴露 shell、任意 HTTP、数据库写权限；
- 将指令式内容标记为安全事件，而不是执行。

Demo 已有基础脱敏和指令式文本标记，但没有企业 DLP、IAM 或隔离沙箱。

## 10. Token、成本与延迟预算

为每个事故建立预算，例如：

- 最大模型调用次数；
- 最大输入/输出 Token；
- 最大工具调用次数；
- 单工具超时和全链路 deadline；
- 按租户/环境的日预算；
- 是否允许启用多 Agent 审核。

优化顺序建议：

1. 异常门控，正常请求零模型调用；
2. 先聚合模板频次，再按 ID 取少量原文；
3. 用 Trace/错误模板确定候选范围；
4. 缓存事故摘要、runbook 和历史案例检索；
5. 小模型做分类、路由和摘要，大模型只处理高价值疑难事故；
6. 把长日志存储在工具端，Agent 只携带引用；
7. 多 Agent 仅在严重事故或低置信度时启用；
8. 上下文过大时分层汇总，并保存汇总所对应的事件 ID 集合。

Token 预算必须和诊断质量一起评测。单纯减少 Token 可能导致漏掉早期根因，单纯增加上下文也可能制造噪声并降低定位能力。

## 11. 降级、容错与 SLO

建议定义清晰的降级路径：

```text
完整多 Agent
  -> 单 Agent（审核服务超时）
  -> 规则诊断 + runbook（模型不可用/预算耗尽）
  -> 仅异常摘要和原始检索链接（数据不全）
  -> 人工排障队列
```

关键机制包括：

- 模型和工具独立超时、有限重试、指数退避；
- 外部依赖断路器与并发舱壁；
- 幂等 incident/run ID，防止重复报告或重复动作；
- 达到预算立刻停止，并返回已有证据与缺失项；
- 某一信号缺失时明确降级置信度；
- 模型输出不合法时重试一次结构修复，仍失败则回退规则报告；
- 绝不因诊断系统故障阻塞业务日志主链路。

可观察 SLO 示例：门控 p95 延迟、诊断 p95/p99 延迟、报告成功率、工具超时率、无效引用率、每事故成本、人工接受率和误导性高置信结论率。

## 12. 人审与修复安全

默认把系统定位为“诊断 Copilot”，而不是自主运维机器人。报告应提供：

- 每项结论对应的证据链接；
- 置信度及其来源；
- 缺少哪些指标或 Trace；
- 建议操作的风险、影响范围和回滚方式；
- “接受 / 驳回 / 修改根因 / 补充证据”的反馈入口。

若后续允许自动动作，必须按风险分级：

- L0：只读查询，可自动；
- L1：创建工单、通知，可在策略允许时自动；
- L2：限流、切流、重启单实例，需要人工审批；
- L3：数据库变更、批量删除、密钥轮换，双人审批或禁止 Agent 执行。

每个写操作需要 allowlist、参数校验、幂等性、dry-run、影响预估、审批令牌、执行审计和可验证回滚。当前 Demo 没有任何写工具，这是有意的安全边界。

## 13. 评测与上线流程

### 13.1 离线数据集

从历史事故中构建脱敏样本：事故时间范围、确认根因、关键证据 ID、影响服务、最终修复。标签由值班记录和复盘报告生成，至少双人抽检。负样本要包含“有 ERROR 但无真实事故”的噪声场景。

防止数据泄漏：按时间切分训练/验证/测试；同一事故不同片段不能跨集合；Prompt、规则和 runbook 调整后保留固定回归集。

### 13.2 指标

- 门控：异常 Precision、Recall、F1、误报/漏报成本；
- RCA：Top-1、Top-3 根因命中率；
- 证据：Citation validity、evidence precision/recall；
- 解释：SRE 认为“结论由证据支持”的比例；
- 行动：建议接受率、平均定位时间 MTTR 的变化；
- 系统：延迟、成本、失败/降级率；
- 安全：敏感信息泄漏率、越权工具调用率、注入测试通过率。

模型置信度不是天然校准概率。应按置信区间分桶比较真实正确率，做 reliability curve；未校准前只能称“启发式置信分数”。

### 13.3 上线阶段

1. **离线回放**：只跑历史数据，不影响值班；
2. **Shadow**：实时生成报告但不通知，只与人工结论比较；
3. **Copilot**：向 SRE 展示，所有操作人工完成；
4. **受控自动化**：仅开放经证明安全、可回滚的 L0/L1 动作；
5. **持续回归**：模型、Prompt、规则、工具任一版本变化都跑固定评测集。

不要直接从合成 Demo 跳到自动修复。

## 14. 推荐的分阶段改造路线

### 阶段 A：把 Demo 变成可接数据的服务

- FastAPI 封装 `IncidentManager`；
- PostgreSQL 保存事故和报告；
- OpenSearch 工具替换内存 Store；
- 保持所有工具只读；
- 接入 OTel Trace 查询；
- 建立脱敏历史事故回归集。

### 阶段 B：实时化与多信号

- Kafka 接入日志、告警和变更事件；
- Flink 在线模板统计和 Incident 聚合；
- 指标、Trace、拓扑、变更四类工具；
- 建立预算、缓存、超时和降级路径；
- Shadow 对比人工事故结论。

### 阶段 C：生产治理

- 多租户 RBAC/ABAC、DLP、KMS、审计；
- 模型网关和按风险路由；
- Prompt/模型/工具/数据版本追踪；
- SRE 反馈闭环与漂移监控；
- 只在评测证明确有收益时启用额外专家 Agent。

### 阶段 D：谨慎引入自动化

- 先从创建工单、补充查询开始；
- 建立动作风险分级和审批；
- 支持 dry-run、幂等与自动回滚验证；
- 高风险操作长期保留人工决策。

## 15. 面试中可据实说明的设计取舍

- “我没有让大模型直接读取几万行日志，而是先结构化、按 Trace/时间窗聚合，再通过有界工具按需检索。”
- “异常门控在 Agent 之前，正常流量不产生模型调用。”
- “单 Agent 负责召回根因和证据；多 Agent 模式增加独立审核，以调用成本换证据精确率。我用相同场景做了对照评测。”
- “结构化输出只保证格式，最终还要用代码核验证据 ID；日志文本按不可信输入处理。”
- “公开 Loghub 数据缺少完整业务根因链，因此我只用它验证接入、表示压缩和行级基线；RCA 用透明合成案例，并明确这不代表生产泛化。”
- “Demo 已实现离线运行、Trace、测试和可选真实模型适配；Kafka、Flink、ES、OTel 多信号、企业权限与人审闭环是明确的生产演进设计，未冒充已经上线。”

这种表述既能说明工程能力，也保留了重要的事实边界。
