import json
from pathlib import Path
from unittest.mock import patch

import pytest

from usagetrim.core import specialized
from usagetrim.core.benchmark import benchmark_suite
from usagetrim.core.benchmark_baseline import baseline_from_json
from usagetrim.core.benchmark_corpus import load_benchmark_cases
from usagetrim.core.redactor import redact_secrets

CORPUS = Path(__file__).parent / "fixtures" / "benchmarks"


def test_repository_baseline_covers_manifest_cases():
    cases = load_benchmark_cases(CORPUS / "manifest.json")
    baseline = baseline_from_json((CORPUS / "baseline.json").read_bytes().decode("utf-8"))

    assert set(baseline) == {case.case_id for case in cases}


def test_loads_manifest_fields_in_order_without_changing_fixture_bytes():
    entries = json.loads((CORPUS / "manifest.json").read_bytes())
    cases = load_benchmark_cases(CORPUS / "manifest.json")

    assert len(cases) == len(entries)
    for case, entry in zip(cases, entries, strict=True):
        assert case.case_id == entry["id"]
        assert case.command == entry["command"]
        assert case.exit_code == entry["exit_code"]
        assert case.required_signals == tuple(entry["required_signals"])
        assert case.raw_output.encode("utf-8") == (CORPUS / entry["file"]).read_bytes()
        assert case.raw_output.endswith("\n")


def test_loaded_cases_run_directly_in_suite():
    cases = load_benchmark_cases(CORPUS / "manifest.json")
    result = benchmark_suite(cases)

    assert tuple(item.case_id for item in result.results) == tuple(case.case_id for case in cases)
    assert result.signals_preserved and result.exact_recovery
    assert result.results[0].output_tokens < result.results[0].raw_tokens
    assert result.results[1].output_tokens < result.results[1].raw_tokens
    assert result.results[2].output_tokens == result.results[2].raw_tokens


@pytest.mark.parametrize(
    ("case_id", "specialized_filter"),
    [
        ("pytest-verbose-success", None),
        ("pytest-verbose-assertion", None),
        ("tsc-pretty-type-errors", "filter_tsc"),
        ("eslint-stylish-mixed-severity", "filter_eslint"),
        ("pyright-source-diagnostics", "filter_pyright"),
        ("pytest-xdist-captured-failure", None),
        ("vitest-verbose-success", "filter_jest_vitest"),
        ("jest-console-failure", "filter_jest_vitest"),
        ("ruff-full-diagnostics", "filter_ruff"),
        ("mypy-pretty-errors", "filter_mypy"),
        ("tsc-plain-type-errors", "filter_tsc"),
        ("npm-install-success", "filter_npm_install"),
        ("git-diff-lockfile-code", "filter_git_diff"),
        ("git-log-patch-history", "filter_git_log"),
        ("ripgrep-context-matches", "filter_ripgrep_output"),
        ("pip-install-success", "filter_pip_install"),
        ("python-threaded-json-traceback", "filter_traceback"),
        ("directory-scan-dependencies", "filter_directory_scan"),
        ("curl-local-http-error", "filter_curl_http"),
        ("git-status-mixed-worktree", "filter_git_status"),
        ("git-log-stat-history", "filter_git_log"),
        ("ripgrep-json-context", "filter_ripgrep_output"),
        ("pip-resolver-conflict", "filter_pip_install"),
        ("pytest-collection-import-failure", None),
        ("pip-inspect-package-json", "filter_json_output"),
    ],
)
def test_captured_fixtures_preserve_signals_recovery_and_expected_routing(
    case_id, specialized_filter
):
    cases = {case.case_id: case for case in load_benchmark_cases(CORPUS / "manifest.json")}
    case = cases[case_id]
    assert case.raw_output.encode("utf-8").decode("utf-8") == case.raw_output
    assert redact_secrets(case.raw_output) == case.raw_output
    assert all(signal in case.raw_output for signal in case.required_signals)
    if specialized_filter is None:
        routed = specialized.auto_specialize_command_output(case.command, case.raw_output)
        assert routed is None
    else:
        original_filter = getattr(specialized, specialized_filter)
        with patch.object(specialized, specialized_filter, wraps=original_filter) as selected:
            routed = specialized.auto_specialize_command_output(case.command, case.raw_output)
        if specialized_filter == "filter_json_output":
            selected.assert_called_once_with(case.raw_output, command=case.command)
        else:
            selected.assert_called_once_with(case.raw_output)
        assert routed is not None
    result = benchmark_suite((case,)).results[0]
    assert result.signals_preserved and result.exact_recovery
    assert result.raw_tokens > 0 and result.output_tokens > 0


@pytest.fixture
def temporary_manifest(tmp_path):
    entries = json.loads((CORPUS / "manifest.json").read_bytes())
    for entry in entries:
        (tmp_path / entry["file"]).write_bytes((CORPUS / entry["file"]).read_bytes())
    manifest = tmp_path / "manifest.json"

    def write_entries(data):
        manifest.write_text(json.dumps(data), encoding="utf-8")
        return manifest

    return entries, write_entries


def test_preserves_crlf_and_trailing_spaces(temporary_manifest, tmp_path):
    entries, write = temporary_manifest
    fixture = tmp_path / entries[0]["file"]
    original = fixture.read_bytes().replace(b"\n", b"\r\n") + b"  "
    fixture.write_bytes(original)

    assert load_benchmark_cases(write(entries))[0].raw_output.encode("utf-8") == original


def test_rejects_duplicate_id(temporary_manifest):
    entries, write = temporary_manifest
    entries[1]["id"] = entries[0]["id"]
    with pytest.raises(ValueError, match="Duplicate benchmark id"):
        load_benchmark_cases(write(entries))


def test_missing_fixture_file(temporary_manifest):
    entries, write = temporary_manifest
    entries[0]["file"] = "missing.log"
    with pytest.raises(FileNotFoundError):
        load_benchmark_cases(write(entries))


@pytest.mark.parametrize("field", ["id", "file", "command", "exit_code", "required_signals"])
def test_rejects_missing_required_fields(temporary_manifest, field):
    entries, write = temporary_manifest
    del entries[0][field]
    with pytest.raises(ValueError, match="missing required fields"):
        load_benchmark_cases(write(entries))


def test_rejects_unknown_fields(temporary_manifest):
    entries, write = temporary_manifest
    entries[0]["unknown"] = "value"
    with pytest.raises(ValueError, match="unknown fields: unknown"):
        load_benchmark_cases(write(entries))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 1),
        ("file", []),
        ("command", None),
        ("exit_code", True),
        ("exit_code", "1"),
        ("required_signals", "error"),
        ("required_signals", [1]),
    ],
)
def test_rejects_invalid_field_types(temporary_manifest, field, value):
    entries, write = temporary_manifest
    entries[0][field] = value
    with pytest.raises(ValueError):
        load_benchmark_cases(write(entries))


@pytest.mark.parametrize("data", [{}, [None]])
def test_rejects_invalid_manifest_structure(temporary_manifest, data):
    _, write = temporary_manifest
    with pytest.raises(ValueError):
        load_benchmark_cases(write(data))


@pytest.mark.parametrize("path_kind", ["parent", "absolute", "symlink"])
def test_rejects_paths_outside_corpus(temporary_manifest, tmp_path, path_kind):
    entries, write = temporary_manifest
    outside = tmp_path.parent / "outside.log"
    outside.write_bytes((CORPUS / entries[0]["file"]).read_bytes())
    if path_kind == "symlink":
        (tmp_path / "link.log").symlink_to(outside)
        entries[0]["file"] = "link.log"
    else:
        entries[0]["file"] = "../outside.log" if path_kind == "parent" else str(outside)
    with pytest.raises(ValueError, match="inside the corpus"):
        load_benchmark_cases(write(entries))
