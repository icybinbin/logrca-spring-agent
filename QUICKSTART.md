# 从下载到核对一份 Spring 日志

本项目是本地日志辅助排查工具。最低验收目标：导入支持格式的日志，获得报告，按引用ID定位原文件行号，导出报告、证据摘录与执行记录。

## 1. 下载后先运行离线示例

推荐 Python 3.11 或 3.12。打开解压后的项目目录，Windows PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 run_web.py
```

无需激活虚拟环境，也不需要管理员权限。终端显示启动地址后打开 [本地网页](http://127.0.0.1:8501)。按 Ctrl+C 可以停止。

Linux/macOS 的等价命令：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run_web.py
```

网页选择“使用内置示例 → 连接池超时 · Spring 文本”，点击“开始诊断”。离线规则基线的预期根因为“数据库连接池耗尽”。这只验证操作和代码闭环，不验证真实大模型效果。

进入“核对日志证据”：选择 `SPRING-00003`，应看到原文件第3行开始的 Hikari 错误和 Java 堆栈。报告中的直接证据应是获取连接超时，而不是网关503。然后下载全部结果ZIP，确认里面有报告、`source-evidence.json` 和 Trace。

## 2. 用自己的 DeepSeek 密钥运行

先在启动网页的同一个 PowerShell 设置环境变量。如果网页已运行，先 Ctrl+C 停止，再执行：

```powershell
$deepseekKey = Read-Host '请输入 DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $deepseekKey).Password
.\.venv\Scripts\python.exe -X utf8 run_web.py
```

项目只读取进程环境变量，不自动读取 `.env` 文件。网页选择“DeepSeek”，保留模型 `deepseek-flash`、标准诊断和最多1个事故，点击“开始诊断”。设置变化和打开网页本身不会触发付费请求。

成功应满足：生成报告；执行记录中有 `model_response`、`tool_call`、`tool_result` 和审核专家；用量里的 `completed_requests` 大于0；证据能在源日志中找到。错误、Token缺失和无效JSON会明确展示，不能把它们当作正常诊断结果。

密钥只存在本地进程环境中。基础脱敏并非完整的敏感信息识别；调用模型前请确认这份日志可以发送给模型服务商。原始上传文件在临时目录分析后删除，本机 `outputs/web/` 保留报告和Trace。

## 3. 上传你自己的日志

没有自己的日志时，可以直接选择内置的“公开故障日志节选 · HikariCP”，或者上传 `data/public_hikari_1546_excerpt.log`。[来源与样例说明](docs/PUBLIC_SAMPLES.md) 区分公开日志和构造案例；它用于检验流程，未提供最终根因真值。

选择“上传自己的日志”，上传 UTF-8 编码的 `.log/.txt/.jsonl/.json/.csv` 文件，大小不超过5MB。先截取故障前后的一段。默认分析1个事故；发现更多事故时网页会提示剩余数量，可以调整到最多10个。

支持的 Spring 格式：

| 格式 | 示例/要求 |
|---|---|
| 常见 Boot 文本 | `2026-10-13T10:00:00.123+08:00 ERROR 1234 --- [orders] [main] c.example.OrderService : message` |
| 无应用名的 Boot 文本 | `时间 LEVEL PID --- [thread] logger : message`；服务名用文件名兜底 |
| 指定的相关ID文本 | `[application][thread][16或32位十六进制traceId-16位spanId]` |
| 本项目 Logback 示例 | `时间 LEVEL [service] [trace=ID] EVENT_ID - message` |
| ECS/Logstash JSONL | 每行一个JSON对象，包含带时区的 `@timestamp`、`message`、`log.level` 或 `level`；可包含 `service.name`、`traceId/trace.id` 和堆栈 |

JSON对象允许 ECS 的嵌套字段，也允许 `service.name` 等扁平键。JSON时间必须带 `Z` 或时区偏移。缺失 trace_id 时使用60秒时间窗分组，这不证明日志间存在因果关系。文本缺失时区时沿用项目的UTC假设并标记，不适合未经核对就跨时区关联。

不支持任意自定义 Logback 布局、application-group 文本前缀、GELF、整个JSON数组或多行排版JSON。遇到不支持的格式会报解析错误，不能声称“没有异常”。Spring支持 ECS/Logstash 结构化日志的版本，可配置：

```properties
spring.application.name=order-service
logging.file.name=spring.log
logging.structured.format.file=ecs
```

这些字段与格式依据 [Spring 官方日志文档](https://docs.spring.io/spring-boot/reference/features/logging.html)。较旧版本或自定义日志配置需要使用相应的 Logback 配置或适配器。

## 4. 你要人工核对的四件事

1. 报告引用ID能否定位到同一事故的源日志？页面是否显示对应的物理行号和堆栈？
2. 该日志直接说明了什么？“获取连接超时”不能单独证明“存在连接泄漏”。
3. 网关503、重试是不是下游症状？它们不能单独确认数据库根因。
4. 没有直接证据时是否保留未知，模型失败是否明确显示，而不是自动换成规则结果？

网页展示的是基础脱敏后的日志摘录；过长事件可能截断，行号用于在你自己的原文件中定位。下载ZIP包含规范化证据摘录，不包含上传原文件。

## 5. 验收范围

| 验收项 | 当前验证方式 |
|---|---|
| 安装后不依赖整个 Loghub 仓库也可运行 | 独立打包后，在临时目录和独立Python环境验证 |
| 文本/JSON导入、报告、证据行和堆栈 | 自动测试和网页组件测试 |
| 空文件、错编码、错误格式、文件大小上限 | 自动拒绝，不生成伪正常报告 |
| DeepSeek工具循环和失败记录 | 协议模拟测试；付费端到端成功仍需运行者验证 |
| 真实生产诊断准确率 | 未验证；内置案例是构造日志 |

项目是本地工具，启动地址绑定 `127.0.0.1`。没有生产平台的多人权限、实时采集或自动修复能力。

## 6. 测试和 GitHub 文件

```powershell
.\.venv\Scripts\python.exe -m pytest
```

缺失公开Loghub数据时，相应集成测试会跳过，其余测试仍执行。GitHub自动测试仅执行 `-m "not loghub"`。需要公开数据集时另行下载并注明来源，然后设置 `LOGHUB_ROOT`，运行 `python -m pytest -m loghub`。

`scripts/package_project.py` 会按白名单生成独立项目ZIP，保留源码、测试、样例、说明和CI配置，排除密钥文件、本机输出、缓存和完整外部数据集。上传GitHub前先解压这个ZIP走一遍上述流程，项目验证范围按实际记录填写。
