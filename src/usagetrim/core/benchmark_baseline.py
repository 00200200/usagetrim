"""Deterministic per-case compression baselines and comparisons."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal

from usagetrim.core.benchmark import BenchmarkSuiteResult

BenchmarkBaseline = dict[str, float]
ComparisonStatus = Literal["acceptable", "regression", "missing", "new"]
REGRESSION_THRESHOLD_PP = 2.0
REGRESSION_ABS_TOLERANCE_PP = 1e-12


@dataclass(frozen=True)
class BenchmarkCaseComparison:
    case_id: str
    status: ComparisonStatus
    baseline_reduction_pct: float | None
    current_reduction_pct: float | None
    drop_pp: float | None


@dataclass(frozen=True)
class BenchmarkComparison:
    results: tuple[BenchmarkCaseComparison, ...]

    @property
    def has_regressions(self) -> bool:
        """Only a measured quality drop counts as a regression, not missing coverage."""
        return any(result.status == "regression" for result in self.results)


def _validate_baseline(data: object) -> BenchmarkBaseline:
    if not isinstance(data, dict):
        raise ValueError("Benchmark baseline must be an object mapping case IDs to percentages")
    for case_id, value in data.items():
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Baseline case IDs must be nonempty strings")
        if type(value) not in (int, float):
            raise ValueError(f"Case {case_id}: reduction_pct must be a number")
        try:
            valid = math.isfinite(value) and value <= 100
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"Case {case_id}: reduction_pct must be finite and at most 100")
    return dict(data)


def create_baseline(suite: BenchmarkSuiteResult) -> BenchmarkBaseline:
    """Extract unrounded case percentages; reject ambiguous duplicate case IDs."""
    baseline: BenchmarkBaseline = {}
    for result in suite.results:
        if result.case_id in baseline:
            raise ValueError(f"Duplicate benchmark case ID: {result.case_id}")
        baseline[result.case_id] = result.reduction_pct
    return _validate_baseline(baseline)


def compare_baseline(
    baseline: BenchmarkBaseline, current: BenchmarkSuiteResult
) -> BenchmarkComparison:
    """Compare each case independently; only drops greater than 2 pp regress.

    Results are sorted by case ID. New and missing cases have no measured drop
    and are reported separately. Neither suite aggregates nor latency are gates.
    """
    baseline = _validate_baseline(baseline)
    current_values = create_baseline(current)
    results = []
    for case_id in sorted(baseline.keys() | current_values.keys()):
        before = baseline.get(case_id)
        after = current_values.get(case_id)
        drop = None
        if before is None:
            status: ComparisonStatus = "new"
        elif after is None:
            status = "missing"
        else:
            drop = before - after
            exceeds_threshold = drop > REGRESSION_THRESHOLD_PP and not math.isclose(
                drop,
                REGRESSION_THRESHOLD_PP,
                rel_tol=0.0,
                abs_tol=REGRESSION_ABS_TOLERANCE_PP,
            )
            status = "regression" if exceeds_threshold else "acceptable"
        results.append(BenchmarkCaseComparison(case_id, status, before, after, drop))
    return BenchmarkComparison(tuple(results))


def baseline_to_json(baseline: BenchmarkBaseline) -> str:
    """Serialize sorted IDs without rounding, latency, or timestamps."""
    return (
        json.dumps(_validate_baseline(baseline), sort_keys=True, indent=2, allow_nan=False) + "\n"
    )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"Duplicate baseline case ID: {key}")
        data[key] = value
    return data


def baseline_from_json(text: str) -> BenchmarkBaseline:
    """Read and validate a baseline, including duplicate JSON object keys."""
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid benchmark baseline JSON: {exc.msg}") from exc
    return _validate_baseline(data)
