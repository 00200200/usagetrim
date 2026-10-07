"""Load explicitly listed repository benchmark fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from usagetrim.core.benchmark import BenchmarkCase


def load_benchmark_cases(manifest_path: Path) -> tuple[BenchmarkCase, ...]:
    """Load cases in manifest order, preserving UTF-8 fixture contents exactly.

    Fixture paths must be relative to the manifest directory and resolve inside
    it, including symlink targets. Invalid metadata raises ValueError; filesystem
    and decoding errors propagate. No fixture discovery or filtering is performed.
    """
    entries = json.loads(manifest_path.read_bytes().decode("utf-8"))
    if not isinstance(entries, list):
        raise ValueError("Benchmark manifest must be a JSON array")
    corpus_dir = manifest_path.resolve().parent
    required = {"id", "file", "command", "exit_code", "required_signals"}
    seen_ids: set[str] = set()
    cases = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Case {index}: expected an object")
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"Case {index}: missing required fields: {', '.join(sorted(missing))}")
        unknown = entry.keys() - required
        if unknown:
            raise ValueError(f"Case {index}: unknown fields: {', '.join(sorted(unknown))}")
        for field in ("id", "file", "command"):
            if not isinstance(entry[field], str):
                raise ValueError(f"Case {index}: {field} must be a string")
        if not entry["id"] or not entry["file"]:
            raise ValueError(f"Case {index}: id and file must not be empty")
        if entry["id"] in seen_ids:
            raise ValueError(f"Duplicate benchmark id: {entry['id']}")
        if entry["exit_code"] is not None and type(entry["exit_code"]) is not int:
            raise ValueError(f"Case {index}: exit_code must be an integer or null")
        signals = entry["required_signals"]
        if not isinstance(signals, list) or any(not isinstance(signal, str) for signal in signals):
            raise ValueError(f"Case {index}: required_signals must be an array of strings")
        relative_path = Path(entry["file"])
        fixture_path = (corpus_dir / relative_path).resolve()
        if relative_path.is_absolute() or not fixture_path.is_relative_to(corpus_dir):
            raise ValueError(f"Case {index}: fixture path must stay inside the corpus directory")
        raw_output = fixture_path.read_bytes().decode("utf-8")
        cases.append(
            BenchmarkCase(
                case_id=entry["id"],
                raw_output=raw_output,
                command=entry["command"],
                required_signals=tuple(signals),
                exit_code=entry["exit_code"],
            )
        )
        seen_ids.add(entry["id"])
    return tuple(cases)
