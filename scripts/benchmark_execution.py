"""Informational baseline of current entry points; no runner or performance gates.

From the checkout: PYTHONPATH=src python scripts/benchmark_execution.py
Defaults to 1 MiB per entry point. --size-mib 16 is explicitly opt-in.
All execution caches and response files are temporary. Tokenizer assets should
already be available locally. RSS excludes the observer and producer processes.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "tests/fixtures/execution/producer.py"


def peak_rss_bytes():
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return value
        if sys.platform.startswith("linux"):
            return value * 1024
    except (ImportError, OSError):
        pass
    return None  # No psutil; unsupported platforms are explicitly unmeasured.


def worker(entry_point, stats, size):
    # Instrument only this benchmark worker, without changing execution code.
    def finish():
        stats.write_text(json.dumps({"parent_peak_rss_bytes": peak_rss_bytes()}), encoding="utf-8")

    atexit.register(finish)
    sys.argv = (
        [
            "usagetrim",
            "run",
            "--",
            sys.executable,
            str(PRODUCER),
            "--mode",
            "records",
            "--bytes",
            str(size),
        ]
        if entry_point == "cli"
        else ["usagetrim", "mcp", "--profile", "full"]
    )
    from usagetrim.cli import main

    main()


def measure(entry_point, size):
    with TemporaryDirectory(prefix="usagetrim-execution-baseline-") as temporary:
        directory = Path(temporary)
        stats = directory / "stats.json"
        env = os.environ | {
            "PYTHONPATH": str(ROOT / "src"),
            "USAGETRIM_CACHE_DIR": str(directory / "cache"),
            "USAGETRIM_STATE_DIR": str(directory / "state"),
            "USAGETRIM_CACHE_MAX_BYTES": str(500 * 1024 * 1024),
            "USAGETRIM_SPILL_BYTES": str(20 * 1024),
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
        }
        request = None
        if entry_point == "mcp":
            argv = [sys.executable, str(PRODUCER), "--mode", "records", "--bytes", str(size)]
            command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            request = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "usagetrim_exec", "arguments": {"command": command}},
                    }
                )
                + "\n"
            ).encode()
        with (
            (directory / "stdout").open("wb") as stdout,
            (directory / "stderr").open("wb") as stderr,
        ):
            start = time.perf_counter()
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--_worker",
                    entry_point,
                    "--_stats",
                    str(stats),
                    "--size-mib",
                    str(size // (1024 * 1024)),
                ],
                input=request,
                stdout=stdout,
                stderr=stderr,
                cwd=directory,
                env=env,
            )
            elapsed = time.perf_counter() - start
        if process.returncode:
            raise RuntimeError(
                f"{entry_point} baseline failed with process status {process.returncode}"
            )
        wire = (directory / "stdout").read_bytes()
        text = wire.decode("utf-8")
        if entry_point == "mcp":
            result = json.loads(text)["result"]
            if result.get("isError"):
                raise RuntimeError("MCP baseline returned a tool error")
            text = result["content"][0]["text"]
        ref = re.search(r"tc_[a-f0-9]{16}", text)
        recovery_latency, recovery_available = None, None
        if ref:
            from usagetrim.core.cache import ContextCache

            start = time.perf_counter()
            recovered = ContextCache(directory / "cache/cache.db").retrieve(ref.group())
            recovery_latency = time.perf_counter() - start
            recovery_available = not recovered.startswith(
                f"Error: ref ID '{ref.group()}' not found"
            )
        return {
            "entry_point": entry_point,
            "wall_seconds": elapsed,
            "generated_bytes": size,
            "response_bytes": len(text.encode("utf-8")),
            "wire_bytes": len(wire),
            "parent_peak_rss_bytes": json.loads(stats.read_text())["parent_peak_rss_bytes"],
            "recovery_ref_produced": ref is not None,
            "recovery_available": recovery_available,
            # Retrieval success does not verify the full redacted producer output.
            "recovery_complete": None,
            "recovery_latency_seconds": recovery_latency,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, choices=(1, 16), default=1)
    parser.add_argument("--entry-point", choices=("cli", "mcp", "both"), default="both")
    parser.add_argument("--_worker", choices=("cli", "mcp"), help=argparse.SUPPRESS)
    parser.add_argument("--_stats", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    size = args.size_mib * 1024 * 1024
    if args._worker:
        if args._stats is None:
            parser.error("worker requires stats path")
        worker(args._worker, args._stats, size)
        return
    entries = ("cli", "mcp") if args.entry_point == "both" else (args.entry_point,)
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
                "rss_method": "resource.RUSAGE_SELF (Linux/macOS), null otherwise",
                "recovery_latency_scope": "ContextCache construction and full retrieve",
                "results": [measure(entry, size) for entry in entries],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
