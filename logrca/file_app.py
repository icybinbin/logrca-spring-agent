"""Upload, evidence inspection and download boundary for the local web UI.

Raw uploads are temporary. Reports and redacted Trace have isolated run dirs.
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .correlation import build_incidents
from .normalizer import load_loghub_csv
from .pipeline import AnalysisRun, analyze_path
from .security import sanitize_log_text
from .spring_json_parser import parse_spring_json
from .spring_parser import parse_spring_log

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@dataclass
class FileAnalysis:
    filename: str
    run: AnalysisRun
    source_events: dict[str, list[dict[str, Any]]]
    traces: dict[str, str]

    def download_payload(self) -> dict[str, Any]:
        payload = self.run.model_dump(mode="json")
        payload["source_path"] = self.filename
        payload["output_dir"] = None
        payload["run_summary_path"] = "run-summary.json"
        payload["report_paths"] = {r.incident_id: f"reports/{r.incident_id}.json" for r in self.run.reports}
        for report in payload["reports"]:
            report["trace_path"] = f"traces/{report['incident_id']}.jsonl" if report["incident_id"] in self.traces else None
        for incident_id, stats in payload["model_telemetry"].items():
            if "trace_path" in stats:
                stats["trace_path"] = f"traces/{incident_id}.jsonl"
        return payload

    def download_archive(self) -> bytes:
        target = io.BytesIO()
        payload = self.download_payload()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("run-summary.json", json.dumps(payload, ensure_ascii=False, indent=2))
            for report in payload["reports"]:
                archive.writestr(payload["report_paths"][report["incident_id"]], json.dumps(report, ensure_ascii=False, indent=2))
            for incident_id, text in self.traces.items():
                archive.writestr(f"traces/{incident_id}.jsonl", text)
            archive.writestr("source-evidence.json", json.dumps(self.source_events, ensure_ascii=False, indent=2))
        return target.getvalue()


def analyze_upload(content: bytes, filename: str, *, output_root: str | Path,
    backend: str = "offline", model: str = "deepseek-flash", mode: str = "multi", max_incidents: int = 1) -> FileAnalysis:
    if not content or not content.strip():
        raise ValueError("日志文件为空，请选择包含日志的文件。")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("日志超过5MB；请先截取故障附近的一段日志。")
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("日志必须采用 UTF-8 编码，请在编辑器中转换后再上传。") from None
    basename = re.split(r"[/\\]", filename)[-1]
    basename = re.sub(r"[^\w.\-]", "_", basename)[:120] or "spring.log"
    if Path(basename).suffix.lower() not in {".log", ".txt", ".jsonl", ".ndjson", ".json", ".csv"}:
        raise ValueError("请上传 .log/.txt/.jsonl/.json/.csv 格式的日志文件。")
    output = Path(output_root).resolve() / uuid.uuid4().hex
    source_events: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="logrca-upload-") as directory:
        source = Path(directory) / "uploaded" / basename
        source.parent.mkdir()
        source.write_bytes(content)
        run = analyze_path(source, output_dir=output, backend=backend, model=model,
            mode=mode, max_incidents=max_incidents)
        # Local re-parse for source inspection; no additional model request.
        if not any(error.stage == "parse" for error in run.errors):
            loader = {"spring": parse_spring_log, "spring_json": parse_spring_json,
                "loghub_csv": load_loghub_csv}[run.detected_format]
            selected = build_incidents(loader(source).events)[:max_incidents]
            for incident in selected:
                source_events[incident.incident_id] = []
                for event in incident.events:
                    message, flags = sanitize_log_text(event.message)
                    source_events[incident.incident_id].append({
                        "line_id": event.line_id, "source_line": event.metadata.get("start_line"),
                        "end_line": event.metadata.get("end_line"), "timestamp": event.timestamp.isoformat(),
                        "service": event.service, "level": event.level, "trace_id": event.trace_id,
                        "message": message, "security_flags": flags,
                        "timezone_assumed": event.metadata.get("timezone_assumed", False),
                    })
    traces = {}
    trace_paths = {report.incident_id: report.trace_path for report in run.reports}
    trace_paths.update({incident_id: stats.get("trace_path") for incident_id, stats in run.model_telemetry.items()})
    for incident_id, path in trace_paths.items():
        if path and Path(path).is_file():
            traces[incident_id] = Path(path).read_text(encoding="utf-8")
    run.source_path = basename
    if run.run_summary_path:
        Path(run.run_summary_path).write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return FileAnalysis(basename, run, source_events, traces)
