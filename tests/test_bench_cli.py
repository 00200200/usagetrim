import json
from dataclasses import replace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from usagetrim import cli
from usagetrim.core.benchmark import BenchmarkResult, BenchmarkSuiteResult

runner = CliRunner()


def test_bench_success_outside_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "console", Console(width=160))
    result = runner.invoke(cli.app, ["bench"], terminal_width=160)
    assert result.exit_code == 0
    for text in ("pytest-failure", "cargo-success", "passthrough", "TOTAL", "Signals", "Recovery"):
        assert text in result.stdout
    assert "PASS" in result.stdout


def test_bench_json_has_case_metrics_and_suite_totals():
    result = runner.invoke(cli.app, ["bench", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["passed"] and data["comparison"] is None
    suite = data["suite"]
    assert [case["case_id"] for case in suite["results"]] == [
        "pytest-failure",
        "cargo-success",
        "passthrough",
    ]
    assert suite["raw_tokens"] == sum(case["raw_tokens"] for case in suite["results"])
    assert suite["output_tokens"] == sum(case["output_tokens"] for case in suite["results"])
    assert suite["signals_preserved"] and suite["exact_recovery"]
    assert suite["latency_ms"] >= 0
    assert result.stdout == json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n"


@pytest.fixture
def measured_suite(monkeypatch):
    case = BenchmarkResult("case", 100, 22, 78.0, 1.0, True, (), True)
    suite = BenchmarkSuiteResult((case,), 100, 22, 78.0, 1.0, True, True)
    monkeypatch.setattr(cli, "benchmark_suite", lambda cases: suite)
    return suite


@pytest.mark.parametrize(
    ("before", "status", "exit_code"), [(80.0, "acceptable", 0), (80.1, "regression", 1)]
)
def test_bench_baseline_threshold(tmp_path, measured_suite, before, status, exit_code):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"case": before}), encoding="utf-8")
    result = runner.invoke(cli.app, ["bench", "--json", "--baseline", str(path)])
    assert result.exit_code == exit_code
    data = json.loads(result.stdout)
    assert data["comparison"]["results"][0]["status"] == status
    assert data["passed"] is (exit_code == 0)


def test_bench_new_and_missing_cases_are_visible_without_failure(tmp_path, measured_suite):
    path = tmp_path / "baseline.json"
    path.write_text('{"removed": 80.0}', encoding="utf-8")
    result = runner.invoke(cli.app, ["bench", "--baseline", str(path)])
    assert result.exit_code == 0
    assert "new" in result.stdout and "missing" in result.stdout
    assert "removed" in result.stdout


@pytest.mark.parametrize("field", ["signals_preserved", "exact_recovery"])
def test_bench_failed_checks_exit_nonzero(monkeypatch, measured_suite, field):
    failed_case = replace(measured_suite.results[0], **{field: False})
    failed_suite = replace(measured_suite, results=(failed_case,), **{field: False})
    monkeypatch.setattr(cli, "benchmark_suite", lambda cases: failed_suite)
    result = runner.invoke(cli.app, ["bench", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert not data["passed"]
    assert data["suite"][field] is False


def test_bench_missing_corpus_reports_readable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_BENCHMARK_MANIFEST", tmp_path / "missing.json")
    result = runner.invoke(cli.app, ["bench"])
    assert result.exit_code == 2
    assert "Benchmark corpus is unavailable" in result.stderr
    assert "source checkout" in " ".join(result.stderr.split())
    assert "Traceback" not in result.output


def test_bench_invalid_baseline_reports_readable_error(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("[]", encoding="utf-8")
    result = runner.invoke(cli.app, ["bench", "--baseline", str(path)])
    assert result.exit_code == 2
    assert "Benchmark error:" in result.stderr


def test_bench_and_benchmark_coexist():
    commands = {
        command.name or command.callback.__name__ for command in cli.app.registered_commands
    }
    assert {"bench", "benchmark", "demo"} <= commands
