from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from logrca.ingestion_audit import (
    audit_loghub_root,
    audit_structured_csv,
    discover_structured_csv,
)


LOGHUB_ROOT = Path(os.environ.get("LOGHUB_ROOT", Path(__file__).resolve().parents[2]))


FIELDS = [
    "LineId",
    "Date",
    "Time",
    "Timestamp",
    "Level",
    "Component",
    "Process",
    "Node",
    "Content",
    "ADDR",
    "EventId",
    "EventTemplate",
    "Label",
]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _sample_rows() -> list[dict[str, str]]:
    return [
        {
            "LineId": "1",
            "Date": "2026-09-21",
            "Time": "10:11:12.123",
            "Level": "ERROR",
            "Component": "api-service",
            "Content": "request req-1234567890abcdef failed",
            "EventId": "E1",
            "EventTemplate": "request <*> failed",
            "Label": "anomaly",
        },
        {
            "LineId": "1",
            "Timestamp": "not-a-time",
            "Level": "WARNING",
            "Content": "row without service and template",
            "Label": "-",
        },
        {
            "LineId": "3",
            "Timestamp": "1700000000",
            "Level": "FATAL",
            "Process": "worker-7",
            "Content": "processing blk_-991",
            "EventId": "E3",
            "Label": "anomaly",
        },
    ]


def test_single_csv_reports_contract_coverage_and_quality(tmp_path: Path) -> None:
    path = tmp_path / "BGL" / "BGL_2k.log_structured.csv"
    _write_csv(path, _sample_rows())
    before = path.read_bytes()

    stats = audit_structured_csv(path)

    assert path.read_bytes() == before  # audit is read-only
    assert stats.dataset == "BGL"
    assert stats.rows == 3
    assert stats.time_parse_success == 2
    assert stats.time_row_number_fallback == 1
    assert stats.parser_strategy_distribution == {
        "date_time_iso_millis": 1,
        "row_number_fallback": 1,
        "timestamp_unix_seconds": 1,
    }
    assert stats.time_format_distribution == stats.parser_strategy_distribution
    assert stats.level_field_present == 3
    assert stats.level_normalization_changes == 2
    assert stats.normalized_level_distribution == {
        "CRITICAL": 1,
        "ERROR": 1,
        "WARN": 1,
    }
    assert stats.service_field_present == 2
    assert stats.service_source_distribution == {"Component": 1, "Process": 1}
    assert stats.trace_id_present == 2
    assert stats.event_id_present == 2
    assert stats.event_template_present == 1
    assert stats.template_or_event_id_present == 2
    assert stats.line_id_present == 3
    assert stats.duplicate_line_ids == 1
    assert stats.duplicate_line_id_values == ["1"]
    assert stats.label_distribution == {"-": 1, "anomaly": 2}
    assert stats.coverage()["time_parse"] == pytest.approx(2 / 3)


def test_root_scan_discovers_only_expected_shape_and_aggregates(tmp_path: Path) -> None:
    first = tmp_path / "BGL" / "BGL_2k.log_structured.csv"
    second = tmp_path / "HDFS" / "HDFS_2k.log_structured.csv"
    ignored = tmp_path / "nested" / "too" / "deep_structured.csv"
    _write_csv(first, _sample_rows())
    _write_csv(second, _sample_rows()[:2])
    _write_csv(ignored, _sample_rows())

    discovered = discover_structured_csv(tmp_path)
    report = audit_loghub_root(tmp_path)

    assert discovered == [first, second]
    assert [item.dataset for item in report.datasets] == ["BGL", "HDFS"]
    assert report.total_files == 2
    assert report.total_rows == 5
    assert report.total_time_parse_success == 3
    assert report.total_time_row_number_fallback == 2
    assert report.total_duplicate_line_ids == 2
    assert report.total_errors == 0
    dumped = json.loads(report.to_json())
    assert dumped["total_rows"] == 5
    assert len(dumped["datasets"]) == 2


def test_limit_is_per_dataset_and_zero_reads_only_header(tmp_path: Path) -> None:
    for dataset in ("A", "B"):
        _write_csv(tmp_path / dataset / f"{dataset}_structured.csv", _sample_rows())

    limited = audit_loghub_root(tmp_path, limit=1)
    header_only = audit_loghub_root(tmp_path, limit=0)

    assert limited.total_rows == 2
    assert [item.rows for item in limited.datasets] == [1, 1]
    assert header_only.total_rows == 0
    assert all(item.coverage()["time_parse"] == 0.0 for item in header_only.datasets)


def test_missing_values_and_extra_columns_are_reported_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "Odd" / "Odd_structured.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "LineId,Content\n"
        "1,plain message\n"
        "2,message,unexpected,columns\n",
        encoding="utf-8",
    )

    stats = audit_structured_csv(path)

    assert stats.rows == 2
    assert stats.time_row_number_fallback == 2
    assert stats.normalized_level_distribution == {"INFO": 2}
    assert stats.label_distribution == {"<missing>": 2}
    assert stats.errors == ["row 2: 2 extra column(s) beyond header"]


def test_invalid_arguments_are_explicit(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover_structured_csv(tmp_path / "missing")
    with pytest.raises(NotADirectoryError):
        file_path = tmp_path / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        discover_structured_csv(file_path)
    with pytest.raises(ValueError, match="non-negative"):
        audit_loghub_root(tmp_path, limit=-1)


def test_audit_uses_same_observed_loghub_time_strategies_as_normalizer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Formats" / "Formats_structured.csv"
    rows = [
        {"LineId": "1", "Time": "Sun Dec 04 04:47:44 2005"},
        {"LineId": "2", "Date": "2015-10-18", "Time": "18:01:47,978"},
        {"LineId": "3", "Time": "20171223-22:15:29:606"},
        {"LineId": "4", "Time": "1077804742"},
        {"LineId": "5", "Date": "17/06/09", "Time": "20:10:40"},
        {"LineId": "6", "Date": "2016-09-28", "Time": "04:30:30"},
        # No year: preserving order via an explicit fallback is safer than
        # manufacturing a date.
        {"LineId": "7", "Date": "03-17", "Time": "16:13:38.811"},
    ]
    _write_csv(path, rows)

    stats = audit_structured_csv(path)

    assert stats.time_parse_success == 6
    assert stats.time_row_number_fallback == 1
    assert stats.parser_strategy_distribution == {
        "date_time_iso_comma_millis": 1,
        "date_time_iso_seconds": 1,
        "date_time_spark_short": 1,
        "row_number_fallback": 1,
        "time_apache_rfc_like": 1,
        "time_healthapp_compact": 1,
        "time_unix_seconds": 1,
    }


@pytest.mark.loghub
@pytest.mark.skipif(len(discover_structured_csv(LOGHUB_ROOT)) < 16,
    reason="16个可选 Loghub 数据集未下载；可设置 LOGHUB_ROOT 后重跑。")
def test_bundled_16_dataset_timestamp_coverage_is_audited() -> None:
    report = audit_loghub_root(LOGHUB_ROOT, limit=2000)
    fallback_datasets = {
        item.dataset
        for item in report.datasets
        if item.time_row_number_fallback == item.rows
    }

    assert report.total_files == 16
    assert report.total_rows == 32_000
    assert report.total_time_parse_success == 22_000
    assert report.total_time_row_number_fallback == 10_000
    # These samples contain month/day/time but no year.  They must remain
    # explicit fallbacks instead of receiving a fabricated year.
    assert fallback_datasets == {"Android", "Linux", "Mac", "OpenSSH", "Proxifier"}
    android = next(item for item in report.datasets if item.dataset == "Android")
    assert android.normalized_level_distribution == {
        "DEBUG": 650,
        "ERROR": 3,
        "INFO": 920,
        "TRACE": 257,
        "WARN": 170,
    }
    assert android.level_normalization_changes == 2000
