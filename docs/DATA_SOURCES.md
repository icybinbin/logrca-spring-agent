# 数据来源与样例边界

`data/spring_db_pool.log`、`data/spring_db_pool.jsonl`、`data/spring_agent_cases.log` 是为学习和功能验证构造的日志，不能当作公司生产故障数据。它们的时间间隔也不代表真实等待耗时。

`data/public_hikari_1546_excerpt.log` 是另行收集的公开故障日志节选，详见 [公开样例与处理记录](PUBLIC_SAMPLES.md)。这份公开报告不属于Loghub，也不能被当作已经确认最终根因的训练标签。

项目支持读取公开 [Loghub](https://github.com/logpai/loghub) 结构化CSV。独立项目ZIP不包含整个Loghub数据集，普通Spring文件分析与核心测试不依赖它。

如果使用或分发Loghub数据，需要保留其原始说明并按适用范围注明：Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. *Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics*. ISSRE, 2023。

上游数据许可原文：

```text
LICENSE OF LOGHUB

The datasets are freely available for research or academic work, subject to
the following condition: For any usage or distribution of the loghub datasets,
please refer to the loghub repository URL (https://github.com/logpai/loghub)
and cite the following loghub paper where applicable.

Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. Loghub: A Large
Collection of System Log Datasets for AI-driven Log Analytics. In ISSRE, 2023.

The above license notice shall be included in all copies of the datasets.
```

此说明是外部数据归属记录，不把数据许可解释为本项目源码的许可，也不代替源码作者选择发布许可。
