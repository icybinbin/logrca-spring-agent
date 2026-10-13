from __future__ import annotations

from logrca.cli import build_parser, main


def test_parser_exposes_all_p0_entry_points() -> None:
    parser = build_parser()

    assert parser.parse_args(["p0-benchmark"]).command == "p0-benchmark"
    assert parser.parse_args(["audit-loghub", "--root", "."]).command == "audit-loghub"
    assert parser.parse_args(["analyze", "--path", "sample.log"]).command == "analyze"
    live = parser.parse_args(["live-benchmark", "--model", "explicit-model"])
    assert live.command == "live-benchmark"
    assert live.model == "explicit-model"
    assert live.repeats == 3


def test_live_benchmark_cli_refuses_to_fake_a_run_without_key(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    output = tmp_path / "must-not-exist.json"

    exit_code = main(
        [
            "live-benchmark",
            "--model",
            "explicit-model",
            "--case-count",
            "100",
            "--max-cases",
            "1",
            "--repeats",
            "1",
            "--bootstrap-iterations",
            "0",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert "真实模型基准未运行" in capsys.readouterr().out
    assert not output.exists()
