from __future__ import annotations

from pathlib import Path
import os
import pytest

from logrca.evaluation import evaluate_bgl_level_baseline
from logrca.correlation import build_incidents
from logrca.detector import AnomalyDetector
from logrca.normalizer import (
    TIME_FALLBACK_STRATEGY,
    compression_stats,
    load_loghub_csv,
    parse_time_with_strategy,
)
from logrca.store import IncidentStore


ROOT = Path(os.environ.get("LOGHUB_ROOT", Path(__file__).resolve().parents[2]))


def requires_loghub(*datasets):
    def decorate(function):
        missing = [name for name in datasets if not (ROOT / name / f"{name}_2k.log_structured.csv").is_file()]
        return pytest.mark.loghub(pytest.mark.skipif(bool(missing),
            reason="可选 Loghub 数据未下载；可设置 LOGHUB_ROOT 后重跑集成测试。")(function))
    return decorate


@requires_loghub("BGL")
def test_bgl_loader_and_public_label_baseline() -> None:
    path = ROOT / "BGL" / "BGL_2k.log_structured.csv"
    incident = load_loghub_csv(path)
    evaluation = evaluate_bgl_level_baseline(path)
    assert len(incident.events) == 2000
    assert len({event.template_id for event in incident.events}) == 120
    assert evaluation["support_anomaly"] == 143
    assert evaluation["confusion"] == {"tp": 143, "fp": 252, "fn": 0, "tn": 1605}
    assert evaluation["f1"] == 0.5316
    limited = evaluate_bgl_level_baseline(path, limit=100)
    assert limited["rows"] == 100
    assert limited["row_limit"] == 100


@requires_loghub("BGL")
def test_labels_do_not_leak_through_agent_tools() -> None:
    incident = load_loghub_csv(ROOT / "BGL" / "BGL_2k.log_structured.csv", limit=5)
    rows = IncidentStore([incident]).search_logs(incident.incident_id, limit=5)
    assert rows
    assert all("label" not in row for row in rows)


@requires_loghub("BGL", "HDFS", "OpenStack")
def test_template_sequence_reduces_context_on_three_datasets() -> None:
    for directory in ("BGL", "HDFS", "OpenStack"):
        path = ROOT / directory / f"{directory}_2k.log_structured.csv"
        stats = compression_stats(load_loghub_csv(path))
        assert stats.rows == 2000
        assert stats.compact_characters < stats.raw_characters
        assert stats.compact_characters < stats.normalized_json_characters


@requires_loghub("HDFS", "OpenStack")
def test_correlation_ids_are_extracted() -> None:
    hdfs = load_loghub_csv(ROOT / "HDFS" / "HDFS_2k.log_structured.csv", limit=20)
    openstack = load_loghub_csv(
        ROOT / "OpenStack" / "OpenStack_2k.log_structured.csv", limit=20
    )
    assert any(event.trace_id and event.trace_id.startswith("blk_") for event in hdfs.events)
    assert any(event.trace_id and event.trace_id.startswith("req-") for event in openstack.events)


@requires_loghub("OpenStack")
def test_same_correlation_id_stays_in_one_incident() -> None:
    openstack = load_loghub_csv(
        ROOT / "OpenStack" / "OpenStack_2k.log_structured.csv", limit=100
    )
    incidents = build_incidents(openstack.events)
    locations: dict[str, set[str]] = {}
    for incident in incidents:
        for event in incident.events:
            if event.trace_id:
                locations.setdefault(event.trace_id, set()).add(incident.incident_id)
    assert locations
    assert all(len(incident_ids) == 1 for incident_ids in locations.values())


def test_observed_loghub_timestamp_layouts_are_parsed_without_source_switches() -> None:
    cases = [
        ({"Time": "Sun Dec 04 04:47:44 2005"}, "time_apache_rfc_like"),
        (
            {"Date": "2015-10-18", "Time": "18:01:47,978"},
            "date_time_iso_comma_millis",
        ),
        ({"Time": "20171223-22:15:29:606"}, "time_healthapp_compact"),
        ({"Time": "1077804742"}, "time_unix_seconds"),
        ({"Date": "17/06/09", "Time": "20:10:40"}, "date_time_spark_short"),
        (
            {"Date": "2005.11.09", "Time": "12:01:01"},
            "date_time_dotted",
        ),
        (
            {"Date": "2016-09-28", "Time": "04:30:30"},
            "date_time_iso_seconds",
        ),
    ]
    for row, expected_strategy in cases:
        _, strategy = parse_time_with_strategy(row, 9)
        assert strategy == expected_strategy


def test_partial_date_without_year_remains_an_explicit_ordering_fallback() -> None:
    timestamp, strategy = parse_time_with_strategy(
        {"Date": "03-17", "Time": "16:13:38.811"}, 9
    )
    assert strategy == TIME_FALLBACK_STRATEGY
    assert timestamp.timestamp() == 9


@requires_loghub("Apache", "Android")
def test_loaded_events_record_real_vs_fallback_timestamp_strategy() -> None:
    apache = load_loghub_csv(ROOT / "Apache" / "Apache_2k.log_structured.csv", limit=1)
    android = load_loghub_csv(ROOT / "Android" / "Android_2k.log_structured.csv", limit=1)

    assert apache.events[0].metadata["timestamp_parser_strategy"] == "time_apache_rfc_like"
    assert apache.events[0].metadata["timestamp_fallback"] is False
    assert android.events[0].metadata["timestamp_parser_strategy"] == TIME_FALLBACK_STRATEGY
    assert android.events[0].metadata["timestamp_fallback"] is True


@requires_loghub("Android")
def test_android_one_letter_levels_are_normalized_before_anomaly_gating() -> None:
    android = load_loghub_csv(ROOT / "Android" / "Android_2k.log_structured.csv")
    levels = {event.level for event in android.events}

    assert not levels.intersection({"E", "W", "I", "D", "V"})
    assert {"ERROR", "WARN", "INFO", "DEBUG", "TRACE"}.issubset(levels)
    assessment = AnomalyDetector().assess(android)
    assert assessment.error_count == 3
    assert assessment.warning_count == 170
    assert assessment.is_anomaly is True
