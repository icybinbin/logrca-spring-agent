"""Local end-to-end file analysis pipeline.

``analyze_path`` is the small application boundary that joins the otherwise
independent teaching components:

    raw file -> normalize -> correlate -> diagnose -> persist reports

It intentionally remains synchronous and local.  A malformed incident or an
individual write failure is captured in ``errors`` and does not prevent later
incidents from being diagnosed.  Invocation errors (missing path, invalid
limits or unsupported mode/format) are rejected immediately because no useful
run can be created for them.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .agents import IncidentManager
from .correlation import build_incidents
from .models import DiagnosisReport, Incident
from .normalizer import load_loghub_csv
from .spring_parser import parse_spring_log
from .spring_json_parser import parse_spring_json


InputFormat = Literal["spring", "spring_json", "loghub_csv"]
RequestedFormat = Literal["auto", "spring", "spring_json", "logback", "loghub", "csv", "loghub_csv"]
PipelineStage = Literal["detect", "parse", "correlate", "diagnose", "output"]


class PipelineError(BaseModel):
    """One recoverable failure observed during a pipeline run."""

    stage: PipelineStage
    message: str
    exception_type: str
    incident_id: str | None = None


class AnalysisRun(BaseModel):
    """Machine-readable summary returned by :func:`analyze_path`."""

    source_path: str
    detected_format: InputFormat
    mode: Literal["single", "multi"]
    backend: Literal["offline", "deepseek"] = "offline"
    model: str | None = None
    model_limits: dict[str, Any] = Field(default_factory=dict)
    model_telemetry: dict[str, dict[str, Any]] = Field(default_factory=dict)
    limit: int | None = None
    max_incidents: int | None = None
    # Spring counts physical source lines (including stack frames); structured
    # CSV counts parsed data records and excludes the header row.
    input_line_count: int = 0
    event_count: int = 0
    discovered_incident_count: int = 0
    incident_count: int = 0
    skipped_incident_count: int = 0
    successful_incident_count: int = 0
    failed_incident_count: int = 0
    anomaly_count: int = 0
    normal_count: int = 0
    known_root_count: int = 0
    unknown_root_count: int = 0
    reports: list[DiagnosisReport] = Field(default_factory=list)
    errors: list[PipelineError] = Field(default_factory=list)
    report_paths: dict[str, str] = Field(default_factory=dict)
    output_dir: str | None = None
    run_summary_path: str | None = None


def _safe_stem(value: str, *, max_prefix: int = 56) -> str:
    """Return a traversal-safe, collision-resistant file-name component."""

    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("._-")
    while ".." in prefix:
        prefix = prefix.replace("..", ".")
    prefix = (prefix or "item")[:max_prefix].rstrip("._-") or "item"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _write_json(payload: Any, target: Path) -> None:
    """Write JSON atomically so an interrupted run does not leave half a file."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _error(
    stage: PipelineStage,
    exc: Exception,
    *,
    incident_id: str | None = None,
) -> PipelineError:
    # Long parser/library messages are bounded before becoming part of a run
    # artifact.  Newlines are flattened for concise CLI/UI presentation.
    message = re.sub(r"\s+", " ", str(exc)).strip()[:1000]
    return PipelineError(
        stage=stage,
        message=message or "operation failed without an error message",
        exception_type=type(exc).__name__,
        incident_id=incident_id,
    )


def _normalize_requested_format(value: str) -> RequestedFormat:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "auto": "auto",
        "spring": "spring",
        "spring_json": "spring_json",
        "jsonl": "spring_json",
        "json": "spring_json",
        "logback": "logback",
        "text": "spring",
        "txt": "spring",
        "log": "spring",
        "loghub": "loghub",
        "csv": "csv",
        "structured_csv": "loghub_csv",
        "loghub_csv": "loghub_csv",
    }
    try:
        return aliases[normalized]  # type: ignore[return-value]
    except KeyError as exc:
        choices = "auto, spring/logback, loghub/csv"
        raise ValueError(f"unsupported format {value!r}; choose {choices}") from exc


def _detect_format(path: Path, requested: RequestedFormat) -> InputFormat:
    if requested in {"spring", "logback"}:
        return "spring"
    if requested == "spring_json":
        return "spring_json"
    if requested in {"loghub", "csv", "loghub_csv"}:
        return "loghub_csv"
    if path.suffix.lower() == ".csv":
        return "loghub_csv"

    # Extension-less fixtures are common in log repositories.  Inspect only
    # the header; the full file is still parsed exactly once by its adapter.
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        first_line = next((line for line in handle if line.strip()), "")
    if first_line.lstrip().startswith("{") or path.suffix.lower() in {".jsonl", ".ndjson", ".json"}:
        return "spring_json"
    header = {item.strip() for item in next(csv.reader([first_line]), [])}
    if header.intersection({"Content", "Message"}) and header.intersection(
        {"EventId", "EventTemplate", "LineId", "Level"}
    ):
        return "loghub_csv"
    return "spring"


def _count_spring_lines(path: Path, limit: int | None) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for count, _ in enumerate(handle, start=1):
            if limit is not None and count >= limit:
                break
    return count


def _empty_run(
    *,
    source_path: Path,
    detected_format: InputFormat,
    mode: Literal["single", "multi"],
    limit: int | None,
    max_incidents: int | None,
    output_dir: Path | None,
) -> AnalysisRun:
    return AnalysisRun(
        source_path=str(source_path),
        detected_format=detected_format,
        mode=mode,
        limit=limit,
        max_incidents=max_incidents,
        output_dir=str(output_dir) if output_dir is not None else None,
    )


def _prepare_output(output_dir: Path | None, run: AnalysisRun) -> Path | None:
    if output_dir is None:
        return None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "reports").mkdir(parents=True, exist_ok=True)
        (output_dir / "traces").mkdir(parents=True, exist_ok=True)
        return output_dir
    except Exception as exc:  # continue with in-memory reports
        run.errors.append(_error("output", exc))
        return None


def _save_summary(run: AnalysisRun, output_root: Path | None) -> None:
    if output_root is None:
        return
    source_key = f"{run.source_path}|{run.detected_format}|{run.mode}|{run.backend}|{run.model}"
    filename = f"run-{_safe_stem(Path(run.source_path).name)}-{hashlib.sha256(source_key.encode('utf-8')).hexdigest()[:8]}.json"
    target = output_root / filename
    run.run_summary_path = str(target.resolve())
    try:
        _write_json(run.model_dump(mode="json"), target)
    except Exception as exc:
        run.run_summary_path = None
        run.errors.append(_error("output", exc))


def analyze_path(
    path: str | Path,
    *,
    format: str = "auto",
    mode: Literal["single", "multi"] = "multi",
    output_dir: str | Path | None = None,
    max_incidents: int | None = 100,
    limit: int | None = None,
    backend: Literal["offline", "deepseek"] = "offline",
    model: str = "deepseek-flash",
    max_model_requests: int = 12,
    request_timeout: float = 30.0,
) -> AnalysisRun:
    """Analyze one local Spring/Logback text file or Loghub structured CSV.

    ``limit`` bounds physical lines for Spring text and data records for CSV.
    ``max_incidents`` bounds Agent work after correlation; ``None`` means all
    discovered incidents.  The returned reports remain available in memory
    even when no output directory is supplied or persistence fails.
    """

    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"log input does not exist: {source}")
    if not source.is_file():
        raise ValueError(f"log input is not a file: {source}")
    if mode not in {"single", "multi"}:
        raise ValueError("mode must be 'single' or 'multi'")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    if max_incidents is not None and max_incidents <= 0:
        raise ValueError("max_incidents must be positive or None")
    if backend not in {"offline", "deepseek"}:
        raise ValueError("backend must be 'offline' or 'deepseek'")
    if backend == "deepseek":
        from dataclasses import asdict
        from .deepseek_adapter import DeepSeekConfig, DeepSeekIncidentManager

        config = DeepSeekConfig(model=model, max_requests=max_model_requests,
            request_timeout=request_timeout)
        manager = DeepSeekIncidentManager(config)
    else:
        manager = IncidentManager()

    requested_format = _normalize_requested_format(format)
    detected = _detect_format(source, requested_format)
    requested_output = Path(output_dir).expanduser().resolve() if output_dir else None
    run = _empty_run(
        source_path=source,
        detected_format=detected,
        mode=mode,
        limit=limit,
        max_incidents=max_incidents,
        output_dir=requested_output,
    )
    run.backend = backend
    if backend == "deepseek":
        run.model = model
        run.model_limits = asdict(config)
        # This dictionary includes failed incidents and their partial Trace.
        run.model_telemetry = manager.telemetry
    output_root = _prepare_output(requested_output, run)

    try:
        if detected == "loghub_csv":
            parsed = load_loghub_csv(source, limit=limit)
            # A structured row is one input log record, even if quoted Content
            # happens to contain an embedded newline.
            run.input_line_count = len(parsed.events)
        elif detected == "spring_json":
            run.input_line_count = _count_spring_lines(source, limit)
            parsed = parse_spring_json(source, line_limit=limit)
        else:
            run.input_line_count = _count_spring_lines(source, limit)
            parsed = parse_spring_log(source, line_limit=limit)
        run.event_count = len(parsed.events)
    except Exception as exc:
        run.errors.append(_error("parse", exc))
        _save_summary(run, output_root)
        return run

    if not parsed.events:
        run.errors.append(
            PipelineError(
                stage="parse",
                message="no log events were parsed from the input",
                exception_type="EmptyInput",
            )
        )
        _save_summary(run, output_root)
        return run

    try:
        incidents = build_incidents(parsed.events)
    except Exception as exc:
        run.errors.append(_error("correlate", exc))
        _save_summary(run, output_root)
        return run

    run.discovered_incident_count = len(incidents)
    selected = incidents if max_incidents is None else incidents[:max_incidents]
    run.incident_count = len(selected)
    run.skipped_incident_count = len(incidents) - len(selected)

    for incident in selected:
        try:
            report = manager.diagnose(
                incident,
                mode=mode,
                trace_dir=(output_root / "traces") if output_root is not None else None,
            )
        except Exception as exc:
            if backend == "deepseek":
                run.model_telemetry = manager.telemetry
            run.failed_incident_count += 1
            run.errors.append(_error("diagnose", exc, incident_id=incident.incident_id))
            continue

        run.reports.append(report)
        if backend == "deepseek":
            run.model_telemetry = manager.telemetry
        run.successful_incident_count += 1
        if report.is_anomaly:
            run.anomaly_count += 1
            if report.root_cause_code is None:
                run.unknown_root_count += 1
            else:
                run.known_root_count += 1
        else:
            run.normal_count += 1

        if output_root is not None:
            suffix = f"{mode}-deepseek" if backend == "deepseek" else mode
            filename = f"report-{_safe_stem(incident.incident_id)}-{suffix}.json"
            target = output_root / "reports" / filename
            try:
                _write_json(report.model_dump(mode="json"), target)
                run.report_paths[incident.incident_id] = str(target.resolve())
            except Exception as exc:
                run.errors.append(_error("output", exc, incident_id=incident.incident_id))

    _save_summary(run, output_root)
    return run


__all__ = ["AnalysisRun", "PipelineError", "analyze_path"]
