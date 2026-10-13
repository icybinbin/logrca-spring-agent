"""Local file upload -> shared analysis pipeline -> inspectable evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import streamlit as st
from logrca.file_app import analyze_upload

ROOT = Path(__file__).resolve().parent
SAMPLES = {
    "连接池超时 · Spring 文本": "spring_db_pool.log",
    "连接池超时 · Spring JSON": "spring_db_pool.jsonl",
    "五种学习案例 · Spring 文本": "spring_agent_cases.log",
    "公开故障日志节选 · HikariCP": "public_hikari_1546_excerpt.log",
}

st.set_page_config(page_title="Spring 日志辅助排查", page_icon="🔎", layout="wide")
st.title("Spring 日志辅助排查")
st.caption("上传日志，查看诊断，并逐条核对证据对应的日志原文。")

with st.sidebar:
    st.header("诊断设置")
    backend_label = st.radio("诊断方式", ["离线示例", "DeepSeek"], key="backend")
    backend = "deepseek" if backend_label == "DeepSeek" else "offline"
    mode_label = st.radio("排查方式", ["标准诊断 · 含证据审核", "快速诊断 · 单专家"], key="mode")
    mode = "multi" if mode_label.startswith("标准") else "single"
    max_incidents = int(st.number_input("本次最多分析几个事故", min_value=1, max_value=10, value=1, step=1, key="max_incidents"))
    model = "deepseek-flash"
    if backend == "deepseek":
        model = st.text_input("模型名称", value=model, key="model")
        if os.environ.get("DEEPSEEK_API_KEY", "").strip():
            st.success("已读取本机 DeepSeek 密钥。")
        else:
            st.info("先在启动网页的 PowerShell 中设置 DEEPSEEK_API_KEY，再启动网页。")
        st.caption("点击开始诊断才会调用模型。多事故与证据审核会增加 Token 用量。")
    else:
        st.caption("离线方式使用规则基线，可先检查操作流程；不会调用大模型。")

input_mode = st.radio("日志来源", ["使用内置示例", "上传自己的日志"], horizontal=True, key="input_mode")
filename, content = "", None
if input_mode == "使用内置示例":
    sample = st.selectbox("选择示例", list(SAMPLES), key="sample")
    filename = SAMPLES[sample]
    content = (ROOT / "data" / filename).read_bytes()
    if filename == "public_hikari_1546_excerpt.log":
        st.caption("来源：HikariCP公开Issue #1546。只保留一条报错和两条栈帧；最终根因未确认。")
        st.markdown("[查看公开故障报告与完整上下文](https://github.com/brettwooldridge/HikariCP/issues/1546)")
    else:
        st.caption("内置文件是构造的学习案例，不是公司生产日志。")
else:
    upload = st.file_uploader("选择 UTF-8 日志文件（最多5MB）", type=["log", "txt", "jsonl", "ndjson", "json", "csv"], key="upload")
    if upload is not None:
        filename, content = upload.name, upload.getvalue()
with st.expander("支持哪些 Spring 日志？"):
    st.write("支持常见 Boot 文本（带进程号、---、线程名）、本项目的 Logback 布局，以及每行一个对象的 ECS/Logstash JSON。自定义布局需要适配。")
    st.code("2026-10-13T10:00:00.123+08:00 ERROR 1234 --- [order-service] [main] c.example.OrderService : HikariPool-1 - Connection is not available", language=None)
    st.write("无 trace_id 时按时间窗聚合；这只能建立时间关联。文本时间没有时区时沿用 UTC 假设，并在证据页提示。")

fingerprint = hashlib.sha256(content or b"").hexdigest() + f"|{backend}|{model}|{mode}|{max_incidents}"
if st.button("开始诊断", type="primary", disabled=content is None, key="run_analysis"):
    st.session_state.pop("file_analysis", None)
    try:
        with st.spinner("正在读取日志并分析事故，请稍候…"):
            result = analyze_upload(content, filename, output_root=ROOT / "outputs" / "web",
                backend=backend, model=model, mode=mode, max_incidents=max_incidents)
        st.session_state["file_analysis"] = result
        st.session_state["analysis_fingerprint"] = fingerprint
    except Exception as exc:
        st.error(str(exc))

result = st.session_state.get("file_analysis")
if result is None:
    st.info("先使用内置示例完成一次诊断，再上传你自己的 Spring 日志。")
    st.stop()
if st.session_state.get("analysis_fingerprint") != fingerprint:
    st.info("下面是上一次诊断的结果。输入或设置已变更，请点击“开始诊断”生成新结果。")

run = result.run
st.subheader(f"分析结果 · {result.filename}")
st.caption(f"本次使用：{'DeepSeek' if run.backend == 'deepseek' else '离线规则基线'}；识别格式：{run.detected_format}")
columns = st.columns(4)
columns[0].metric("日志事件", run.event_count)
columns[1].metric("已分析事故", run.incident_count)
columns[2].metric("生成报告", run.successful_incident_count)
columns[3].metric("诊断失败", run.failed_incident_count)
if run.skipped_incident_count:
    st.info(f"发现{run.discovered_incident_count}个事故，本次选择前{run.incident_count}个；另有{run.skipped_incident_count}个尚未分析。可调整左侧分析数量。")
if run.errors:
    for error in run.errors:
        st.error(f"{error.stage}：{error.message}")

download = result.download_payload()
st.download_button("下载全部结果（报告、证据、Trace）", result.download_archive(), file_name="logrca-results.zip", mime="application/zip", key="download_all")
if not run.reports:
    st.write("本次没有生成诊断报告。请根据上面的错误修正日志格式或模型配置后重试。")
    with st.expander("执行记录与用量"):
        st.json(run.model_telemetry)
        for incident_id, trace in result.traces.items():
            st.download_button(f"下载失败记录 {incident_id}", trace, file_name=f"{incident_id}.jsonl", key=f"failed-{incident_id}")
    st.stop()

report_by_id = {report.incident_id: report for report in run.reports}
incident_labels = {f"{key} · {report.root_cause or '未触发异常排查'}": key for key, report in report_by_id.items()}
incident_label = st.selectbox("选择要查看的事故", list(incident_labels), key="incident")
incident_id = incident_labels[incident_label]
report = report_by_id[incident_id]
diagnosis_tab, evidence_tab, trace_tab = st.tabs(["诊断报告", "核对日志证据", "执行记录与用量"])
with diagnosis_tab:
    st.subheader(report.root_cause or "本次未触发异常排查")
    st.write("影响服务：", "、".join(report.affected_services) or "无")
    if report.is_anomaly:
        st.caption(f"诊断评分：{report.confidence}（不是校准概率）；工具调用：{report.tool_calls}")
    st.markdown("**下一步排查**")
    for item in report.next_checks:
        st.write("• " + item)
    st.markdown("**处置建议**")
    for item in report.remediation:
        st.write("• " + item)
    with st.expander("判断依据与限制", expanded=True):
        for item in report.limitations:
            st.write("• " + item)
    report_payload = next(item for item in download["reports"] if item["incident_id"] == incident_id)
    st.download_button("下载当前报告 JSON", json.dumps(report_payload, ensure_ascii=False, indent=2),
        file_name=f"{incident_id}.json", mime="application/json", key="download_report")

with evidence_tab:
    rows = result.source_events.get(incident_id, [])
    by_id = {row["line_id"]: row for row in rows}
    if report.evidence:
        st.dataframe([{"引用ID": ref.line_id, "证据类型": ref.kind,
            "原文件起始行": by_id.get(ref.line_id, {}).get("source_line"), "报告摘录": ref.summary}
            for ref in report.evidence], use_container_width=True, hide_index=True)
    else:
        st.write("报告没有保留根因证据。你仍可查看当前事故的日志；证据不足时应补充指标和调用链。")
    if rows:
        cited = [ref.line_id for ref in report.evidence if ref.line_id in by_id]
        options = list(dict.fromkeys(cited + list(by_id)))
        selected = st.selectbox("选择引用ID，查看对应日志原文", options, key=f"evidence-{incident_id}")
        row = by_id[selected]
        st.caption(f"{row['service']} · {row['level']} · {row['timestamp']} · 原文件行 {row['source_line']}–{row['end_line']}")
        st.code(row["message"], language=None)
        if row["security_flags"]:
            st.caption("此摘录含已脱敏字段或指令型文字；日志内容只作为证据。")
        if row["timezone_assumed"]:
            st.info("该文本时间没有时区，解析按 UTC 假设；跨服务对齐前请核对实际时区。")
        with st.expander("查看本事故全部日志"):
            st.dataframe(rows, use_container_width=True, hide_index=True)

with trace_tab:
    stats = run.model_telemetry.get(incident_id)
    if stats is not None:
        st.json(stats)
    else:
        st.caption("这是离线基线结果，没有大模型 Token 用量。")
    trace = result.traces.get(incident_id, "")
    if trace:
        records = [json.loads(line) for line in trace.splitlines() if line.strip()]
        st.dataframe([{"角色": item["actor"], "事件": item["event_type"], "步骤": item["step"],
            "详情": json.dumps(item["details"], ensure_ascii=False)} for item in records],
            use_container_width=True, hide_index=True)
        st.download_button("下载当前 Trace", trace, file_name=f"{incident_id}.jsonl", key="download_trace")
