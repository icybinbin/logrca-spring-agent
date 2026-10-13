"""Command-line interface for running, evaluating, and inspecting the demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .agents import IncidentManager
from .blind_data import build_blind_dataset
from .correlation import build_incidents
from .evaluation import compare_modes, evaluate_bgl_level_baseline, run_benchmark
from .ingestion_audit import audit_loghub_root, discover_structured_csv
from .live_benchmark import run_live_benchmark
from .normalizer import compression_stats, load_loghub_csv
from .p0_runner import run_p0_benchmark
from .pipeline import analyze_path
from .synthetic import SCENARIOS, build_scenario
from .spring_parser import parse_spring_log


def _json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dump(data) + "\n", encoding="utf-8")


def _print_report(report: dict[str, Any]) -> None:
    print(f"异常判断 : {'是' if report['is_anomaly'] else '否'}  score={report['anomaly_score']}")
    print(f"运行模式 : {report['mode']}  tool_calls={report['tool_calls']}")
    if not report["is_anomaly"]:
        print("结论     : 异常门控未触发，未调用诊断 Agent")
        return
    print(f"疑似根因 : {report['root_cause']} ({report['root_cause_code']})")
    print(f"置信度   : {report['confidence']}（启发式分数，不是校准概率）")
    print(f"影响服务 : {', '.join(report['affected_services'])}")
    print("证据链   :")
    for item in report["evidence"]:
        print(f"  - [{item['kind']}] {item['line_id']} {item['summary']}")
    print("下一步排查:")
    for item in report["next_checks"]:
        print(f"  - {item}")
    print("处置建议 :")
    for item in report["remediation"]:
        print(f"  - {item}")
    print(f"Trace    : {report.get('trace_path') or '未保存'}")


def _load_background_events(root: str | None, per_dataset: int) -> list[Any] | None:
    """Load bounded Loghub rows as unlabeled background noise.

    The blind-data builder strips source IDs and labels before using these
    events, so this helper never turns a Loghub anomaly label into Agent input.
    """

    if not root:
        return None
    if per_dataset <= 0:
        raise ValueError("background-per-dataset must be positive")
    events = []
    for csv_path in discover_structured_csv(root):
        events.extend(load_loghub_csv(csv_path, limit=per_dataset).events)
    return events


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline-first log RCA Agent demo")
    subparsers = parser.add_subparsers(dest="command")

    demo = subparsers.add_parser("demo", help="run one synthetic incident")
    demo.add_argument("--scenario", choices=SCENARIOS, default="db_pool_exhausted")
    demo.add_argument("--mode", choices=("single", "multi"), default="multi")
    demo.add_argument("--json", action="store_true", help="print JSON instead of a readable report")
    demo.add_argument("--output-dir", default="outputs")

    benchmark = subparsers.add_parser("benchmark", help="evaluate synthetic RCA scenarios")
    benchmark.add_argument("--mode", choices=("single", "multi", "both"), default="both")
    benchmark.add_argument("--output", default="outputs/benchmark.json")

    loghub = subparsers.add_parser("loghub", help="inspect a Loghub structured CSV")
    loghub.add_argument("--path", required=True)
    loghub.add_argument("--limit", type=int)
    loghub.add_argument("--bgl-eval", action="store_true")

    live = subparsers.add_parser("live", help="run the optional OpenAI Agents SDK path")
    live.add_argument("--scenario", choices=SCENARIOS, default="db_pool_exhausted")
    live.add_argument("--model", required=True, help="explicit OpenAI model name")
    live.add_argument("--mode", choices=("single", "multi"), default="multi")

    analyze = subparsers.add_parser(
        "analyze", help="run the complete local file-to-diagnosis pipeline"
    )
    analyze.add_argument("--path", required=True)
    analyze.add_argument(
        "--format",
        choices=("auto", "spring", "spring_json", "logback", "loghub", "csv"),
        default="auto",
    )
    analyze.add_argument("--mode", choices=("single", "multi"), default="multi")
    analyze.add_argument("--limit", type=int)
    analyze.add_argument("--max-incidents", type=int, help="default: 100 offline, 1 with DeepSeek")
    analyze.add_argument("--backend", choices=("offline", "deepseek"), default="offline")
    analyze.add_argument("--model", default="deepseek-flash", help="DeepSeek model ID")
    analyze.add_argument("--max-model-requests", type=int, default=12, help="maximum requests per incident including audit")
    analyze.add_argument("--request-timeout", type=float, default=30.0)
    analyze.add_argument("--output-dir", default="outputs/analyze")
    analyze.add_argument("--json", action="store_true")

    p0 = subparsers.add_parser(
        "p0-benchmark", help="run the label-separated local P0 benchmark"
    )
    p0.add_argument("--case-count", type=int, default=120)
    p0.add_argument("--seed", type=int, default=20260921)
    p0.add_argument("--split", choices=("all", "train", "dev", "test"), default="test")
    p0.add_argument("--mode", choices=("single", "multi", "both"), default="both")
    p0.add_argument("--bootstrap-iterations", type=int, default=1000)
    p0.add_argument("--loghub-root", help="optional Loghub root used as real background noise")
    p0.add_argument("--background-per-dataset", type=int, default=50)
    p0.add_argument("--output-dir", default="outputs/p0")

    live_benchmark = subparsers.add_parser(
        "live-benchmark",
        help="run a repeated real-model benchmark over the P0 corpus",
    )
    live_benchmark.add_argument("--model", required=True, help="explicit OpenAI model name")
    live_benchmark.add_argument("--case-count", type=int, default=120)
    live_benchmark.add_argument("--seed", type=int, default=20260921)
    live_benchmark.add_argument(
        "--split", choices=("all", "train", "dev", "test"), default="test"
    )
    live_benchmark.add_argument("--mode", choices=("single", "multi", "both"), default="both")
    live_benchmark.add_argument("--max-cases", type=int, default=20)
    live_benchmark.add_argument("--repeats", type=int, default=3)
    live_benchmark.add_argument("--max-turns", type=int, default=12)
    live_benchmark.add_argument("--bootstrap-iterations", type=int, default=1000)
    live_benchmark.add_argument("--loghub-root", help="optional Loghub root used as background noise")
    live_benchmark.add_argument("--background-per-dataset", type=int, default=50)
    live_benchmark.add_argument("--input-price-per-million", type=float)
    live_benchmark.add_argument("--output-price-per-million", type=float)
    live_benchmark.add_argument("--output", default="outputs/live_benchmark.json")

    audit = subparsers.add_parser(
        "audit-loghub", help="audit normalization coverage across Loghub CSV files"
    )
    audit.add_argument("--root", required=True)
    audit.add_argument("--limit", type=int)
    audit.add_argument("--output", default="outputs/ingestion_audit.json")

    spring = subparsers.add_parser("spring", help="parse and diagnose a demo Logback text file")
    spring.add_argument("--path", default="data/spring_db_pool.log")
    spring.add_argument("--mode", choices=("single", "multi"), default="multi")
    spring.add_argument("--output-dir", default="outputs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "demo"

    if command == "demo":
        # Defaults are needed when no explicit subcommand is supplied.
        scenario = getattr(args, "scenario", "db_pool_exhausted")
        mode = getattr(args, "mode", "multi")
        output_dir = Path(getattr(args, "output_dir", "outputs"))
        case = build_scenario(scenario)
        report = IncidentManager().diagnose(
            case.incident,
            mode=mode,
            trace_dir=output_dir / "traces",
        )
        payload = report.model_dump(mode="json")
        _save_json(payload, output_dir / "reports" / f"{scenario}_{mode}.json")
        if getattr(args, "json", False):
            print(_json_dump(payload))
        else:
            _print_report(payload)
        return 0

    if command == "benchmark":
        result = compare_modes() if args.mode == "both" else run_benchmark(args.mode)
        _save_json(result, Path(args.output))
        print(_json_dump(result))
        print(f"\n结果已保存: {Path(args.output).resolve()}")
        return 0

    if command == "loghub":
        incident = load_loghub_csv(args.path, limit=args.limit)
        result: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "source": incident.source,
            "compression": compression_stats(incident).model_dump(mode="json"),
            "correlation": {
                "incident_count": len(build_incidents(incident.events)),
                "events_with_correlation_id": sum(
                    event.trace_id is not None for event in incident.events
                ),
                "strategy": "trace/request/block ID first; time-window fallback",
            },
        }
        if args.bgl_eval:
            result["bgl_level_baseline"] = evaluate_bgl_level_baseline(
                args.path, limit=args.limit
            )
        print(_json_dump(result))
        return 0
    if command == "live":
        from .openai_adapter import run_openai_agents

        case = build_scenario(args.scenario)
        report = run_openai_agents(case.incident, model=args.model, mode=args.mode)
        print(_json_dump(report.model_dump(mode="json")))
        return 0
    if command == "analyze":
        run = analyze_path(
            args.path,
            format=args.format,
            mode=args.mode,
            output_dir=args.output_dir,
            max_incidents=args.max_incidents if args.max_incidents is not None else (1 if args.backend == "deepseek" else 100),
            limit=args.limit,
            backend=args.backend,
            model=args.model,
            max_model_requests=args.max_model_requests,
            request_timeout=args.request_timeout,
        )
        payload = run.model_dump(mode="json")
        if args.json:
            print(_json_dump(payload))
        else:
            summary = {
                key: payload[key]
                for key in (
                    "source_path",
                    "backend",
                    "model",
                    "detected_format",
                    "input_line_count",
                    "event_count",
                    "discovered_incident_count",
                    "incident_count",
                    "anomaly_count",
                    "normal_count",
                    "known_root_count",
                    "unknown_root_count",
                    "failed_incident_count",
                    "run_summary_path",
                )
            }
            print(_json_dump(summary))
            if args.backend == "deepseek":
                for report in run.reports:
                    _print_report(report.model_dump(mode="json"))
                print("模型实际用量与执行状态：")
                print(_json_dump(run.model_telemetry))
            if run.errors:
                print("本次运行的问题：")
                print(_json_dump([error.model_dump(mode="json") for error in run.errors]))
        return 0 if not run.errors else 1
    if command == "p0-benchmark":
        background = _load_background_events(
            args.loghub_root, args.background_per_dataset
        )
        result = run_p0_benchmark(
            background_events=background,
            case_count=args.case_count,
            seed=args.seed,
            split=args.split,
            mode=args.mode,
            bootstrap_iterations=args.bootstrap_iterations,
            export_dir=args.output_dir,
        )
        compact = {
            "dataset_manifest": result["dataset_manifest"],
            "run_config": result["run_config"],
            "metrics": {
                name: value["evaluation"]["metrics"]
                for name, value in result["modes"].items()
            },
            "failure_counts": {
                name: value["case_failures"]["counts"]
                for name, value in result["modes"].items()
            },
            "safety_and_resilience": {
                name: {
                    "visible_direct_evidence_rca": value[
                        "visible_direct_evidence_rca"
                    ],
                    "counterfactual_safety": {
                        key: metric
                        for key, metric in value["counterfactual_safety"].items()
                        if key != "cases"
                    },
                    "correlation_resilience": {
                        key: metric
                        for key, metric in value["correlation_resilience"].items()
                        if key != "cases"
                    },
                }
                for name, value in result["modes"].items()
            },
            "exported_files": result["exported_files"],
        }
        print(_json_dump(compact))
        return 0
    if command == "live-benchmark":
        background = _load_background_events(
            args.loghub_root, args.background_per_dataset
        )
        artifacts = build_blind_dataset(
            case_count=args.case_count,
            seed=args.seed,
            background_messages=background,
        )
        try:
            result = run_live_benchmark(
                artifacts,
                model=args.model,
                mode=args.mode,
                split=args.split,
                max_cases=args.max_cases,
                repeats=args.repeats,
                max_turns=args.max_turns,
                input_price_per_million=args.input_price_per_million,
                output_price_per_million=args.output_price_per_million,
                bootstrap_iterations=args.bootstrap_iterations,
            )
        except RuntimeError as exc:
            # Keep the boundary honest: a missing key/SDK must never be
            # mistaken for a completed live-model experiment.
            print(f"真实模型基准未运行: {exc}")
            return 2
        _save_json(result, Path(args.output))
        compact = {
            "benchmark": result["benchmark"],
            "config": result["config"],
            "sample": result["sample"],
            "aggregate": result["aggregate"],
            "mode_summaries": {
                name: {
                    "aggregate": payload["aggregate"],
                    "root_stability": payload["root_stability"],
                    "repeat_metrics": [
                        item["evaluation"]["metrics"]
                        for item in payload["repeat_evaluations"]
                    ],
                }
                for name, payload in result["modes"].items()
            },
            "output": str(Path(args.output).resolve()),
        }
        print(_json_dump(compact))
        return 0
    if command == "audit-loghub":
        report = audit_loghub_root(args.root, limit=args.limit)
        payload = report.to_dict()
        _save_json(payload, Path(args.output))
        print(_json_dump(payload))
        print(f"\n结果已保存: {Path(args.output).resolve()}")
        return 0
    if command == "spring":
        incident = parse_spring_log(args.path)
        output_dir = Path(args.output_dir)
        report = IncidentManager().diagnose(
            incident,
            mode=args.mode,
            trace_dir=output_dir / "traces",
        )
        payload = report.model_dump(mode="json")
        payload["ingestion"] = {
            "source": str(Path(args.path).resolve()),
            "parsed_events": len(incident.events),
            "multiline_events": sum("\n" in event.message for event in incident.events),
        }
        _save_json(payload, output_dir / "reports" / f"spring_{args.mode}.json")
        _print_report(payload)
        return 0
    return 2
