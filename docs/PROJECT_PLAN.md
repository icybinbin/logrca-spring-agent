# 项目实施与后续学习计划

> 本文保留早期里程碑计划。当前文件上传、DeepSeek 工具调用和证据核对的使用方式见 [QUICKSTART](../QUICKSTART.md)，已实现能力与限制见 [README](../README.md)。

## 一、这次交付采用的原则

目标不是在本地复刻一个企业级日志平台，而是形成一条可以亲自运行、沿源码解释、用测试证明的最小完整链路。优先级如下：

1. 先保证没有 API Key 也能完整运行。
2. 先做领域契约、工具边界、Agent loop、Trace 和 Eval，再做 UI。
3. 单 Agent 是成本基线，多 Agent 必须通过对照指标证明职责拆分有价值。
4. 公开数据负责真实性，合成数据负责可控 Ground Truth，二者不混用标签。
5. 生产能力只做架构设计，不冒充已经实现或上线。

## 二、里程碑状态

| 里程碑 | 交付物 | 状态 |
|---|---|---|
| M0 最小 Agent 教学版 | `agent_core.py`、`log_tools.py`、`run_stage1.py` | 已完成并保留 |
| M1 数据与领域层 | Pydantic Schema、6 个场景、Loghub 适配、Spring 多行解析、关联与模板表示 | 已完成 |
| M2 Agent 基础设施 | Tool Registry、State、Agent loop、步数限制、Trace、工具白名单 | 已完成 |
| M3 诊断编排 | 异常门控、Hypothesis Agent、Evidence Auditor、Manager、Grounding | 已完成 |
| M4 可信评测 | 120-case 标签侧车 P0、BGL 行级基线、Single/Multi 消融、扰动/反事实/Bootstrap；当前本机 110 个测试通过 | 已完成 |
| M5 可演示性 | CLI、Streamlit、报告与 JSONL Trace | 已完成 |
| M6 可替换真实模型 | Python Manager + 两个 OpenAI Agents SDK 专家适配层 | 代码完成；缺 Key，未实测效果 |
| M7 生产平台 | Kafka/Flink/日志库/OTel/RBAC/人审/灰度 | 只完成设计文档 |

## 三、验收命令

```powershell
python -X utf8 -m pytest
python -X utf8 -m logrca demo --scenario db_pool_exhausted --mode multi
python -X utf8 -m logrca demo --scenario normal --mode multi
python -X utf8 -m logrca spring --path data\spring_db_pool.log --mode multi
python -X utf8 -m logrca benchmark --mode both
python -X utf8 -m logrca loghub --path ..\BGL\BGL_2k.log_structured.csv --bgl-eval
python -X utf8 -m logrca analyze --path data\spring_db_pool.log --mode multi --output-dir outputs\analyze
python -X utf8 -m logrca p0-benchmark --split test --mode both --loghub-root .. --output-dir outputs_p0
python -X utf8 -m logrca audit-loghub --root .. --limit 2000 --output outputs_p0\ingestion_audit.json
streamlit run streamlit_app.py
```

验收标准：

- 测试全部通过；
- 故障场景报告包含根因、存在于事故中的证据 ID、排查和处置建议；
- 正常场景工具调用为 0；
- Multi 的证据 Precision 高于 Single，同时明确显示额外调用成本；
- Trace 能还原所有工具动作且没有完整私有思维链；
- BGL 标签不暴露给 Agent 工具；
- P0 的 Agent 输入不含真值字段，同一直接证据文案/模板不跨 split；
- OOD 与 direct 证据删除案例能安全弃权，trace 污染子评测真正重新执行关联；
- README 明确区分已实现、合成验证和生产设计。

## 四、你回来后的三阶段学习安排

### 第一天：先能口述一次执行链（约 2 小时）

1. 运行故障与正常场景。
2. 对照 Trace 阅读 `models.py`、`runtime.py`。
3. 逐轮口述 Action、Observation、State、FinalAction。
4. 回答：为什么 Tool calling 本身不一定等于 Agent？为什么需要最大步数？

完成标准：不看文档，能画出 Agent loop 并指出项目中每个组件。

### 第二天：理解项目取舍（约 2～3 小时）

1. 阅读 `store.py`、`agents.py`、`detector.py`。
2. 对照 Single/Multi 报告和 benchmark。
3. 完成 `LEARNING_GUIDE.md` 的实验 1～6。
4. 阅读 `EVALUATION.md`，解释为什么 Loghub 不能评 RCA。

完成标准：能解释异常门控、工具白名单、证据审核、Grounding、Token 策略以及多 Agent 的成本收益。

### 第三天：面试化表达（约 2 小时）

1. 阅读 `PRODUCTION.md`，把 Demo 映射到 Kafka/Flink/ES/OTel。
2. 阅读 `INTERVIEW_GUIDE.md`，先自己答再看答案。
3. 用 30 秒、2 分钟、5 分钟三个版本介绍项目。
4. 从简历每一项反向指到源码、测试或评测结果。

完成标准：能主动说明边界，不把合成 1.0、离线规则或可选 SDK 代码夸大成生产效果。

## 五、有真实数据或额度后的升级顺序

1. 获取合法脱敏的事故工单标签，冻结由独立人员标注的时间切分 holdout。
2. 安装可选 SDK，用明确模型跑少量固定集，记录 Token、延迟、费用和 Trace。
3. 与 Offline/Single/Multi 使用同一数据集做消融，不凭主观展示挑案例。
4. 增加并发故障、时钟漂移、长窗口和缺失链路 ID 压力测试，再校准置信度。
5. 最后才考虑向量检索、历史工单记忆、更多专家 Agent 或自动修复；每个新增组件都要证明增益。
