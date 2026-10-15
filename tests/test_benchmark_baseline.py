import pytest

from usagetrim.core.benchmark import BenchmarkResult, BenchmarkSuiteResult
from usagetrim.core.benchmark_baseline import (
    baseline_from_json,
    baseline_to_json,
    compare_baseline,
    create_baseline,
)


def suite(*values):
    results = tuple(
        BenchmarkResult(case_id, 1000, 0, reduction, 999.0, True, (), True)
        for case_id, reduction in values
    )
    # The comparison must ignore aggregate percentages and latency.
    return BenchmarkSuiteResult(results, 0, 0, 100.0, 999.0, True, True)


@pytest.mark.parametrize(
    ("current", "status", "drop"),
    [
        (80.0, "acceptable", 0.0),
        (78.0, "acceptable", 2.0),
        (77.9, "regression", 2.1),
        (77.999999, "regression", 2.000001),
        (85.0, "acceptable", -5.0),
    ],
)
def test_per_case_threshold_uses_unrounded_percentages(current, status, drop):
    comparison = compare_baseline({"case": 80.0}, suite(("case", current)))
    result = comparison.results[0]

    assert result.status == status
    assert result.baseline_reduction_pct == 80.0
    assert result.current_reduction_pct == current
    assert result.drop_pp == pytest.approx(drop)
    assert comparison.has_regressions is (status == "regression")


def test_exact_two_point_drop_from_token_counts_tolerates_float_noise():
    before = 100 * (3 - 2) / 3
    after = 100 * (150 - 103) / 150
    comparison = compare_baseline({"case": before}, suite(("case", after)))

    assert before - after > 2.0
    assert comparison.results[0].status == "acceptable"
    assert comparison.results[0].drop_pp == before - after
    assert not comparison.has_regressions


def test_comparison_reports_missing_new_and_regressing_cases_in_sorted_order():
    comparison = compare_baseline(
        {"removed": 50.0, "regressed": 80.0, "improved": 50.0},
        suite(("regressed", 77.9), ("new", 90.0), ("improved", 100.0)),
    )

    assert [(result.case_id, result.status) for result in comparison.results] == [
        ("improved", "acceptable"),
        ("new", "new"),
        ("regressed", "regression"),
        ("removed", "missing"),
    ]
    new, missing = comparison.results[1], comparison.results[3]
    assert new.baseline_reduction_pct is None and new.current_reduction_pct == 90.0
    assert missing.baseline_reduction_pct == 50.0 and missing.current_reduction_pct is None
    assert new.drop_pp is missing.drop_pp is None
    assert comparison.has_regressions


@pytest.mark.parametrize(
    ("baseline", "current", "statuses"),
    [
        ({}, suite(), []),
        ({}, suite(("new", 0.0)), ["new"]),
        ({"missing": 0.0}, suite(), ["missing"]),
    ],
)
def test_empty_inputs_are_not_quality_regressions(baseline, current, statuses):
    comparison = compare_baseline(baseline, current)
    assert [result.status for result in comparison.results] == statuses
    assert not comparison.has_regressions


def test_baseline_creation_and_json_are_deterministic_and_lossless():
    baseline = create_baseline(suite(("z", -1.123456789), ("a", 80.123456789)))
    text = baseline_to_json(baseline)

    assert text == '{\n  "a": 80.123456789,\n  "z": -1.123456789\n}\n'
    assert baseline_from_json(text) == baseline
    assert baseline_to_json(dict(reversed(list(baseline.items())))) == text
    assert create_baseline(suite()) == baseline_from_json("{}") == {}


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '{"a": {}}',
        '{"a": true}',
        '{"a": "80"}',
        '{"": 0}',
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": 101}',
        '{"a": 0, "a": 1}',
        "{",
    ],
)
def test_rejects_invalid_baseline_json(text):
    with pytest.raises(ValueError):
        baseline_from_json(text)


@pytest.mark.parametrize("baseline", [{1: 0.0}, {"a": float("nan")}, {"a": True}])
def test_serialization_and_comparison_validate_baseline(baseline):
    with pytest.raises(ValueError):
        baseline_to_json(baseline)
    with pytest.raises(ValueError):
        compare_baseline(baseline, suite())


def test_duplicate_current_ids_are_rejected():
    current = suite(("duplicate", 80.0), ("duplicate", 70.0))
    with pytest.raises(ValueError, match="Duplicate benchmark case ID"):
        create_baseline(current)
    with pytest.raises(ValueError, match="Duplicate benchmark case ID"):
        compare_baseline({}, current)
