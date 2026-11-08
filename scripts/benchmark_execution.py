"""Execution baselines and isolated bounded-runner resource measurements.

From the checkout: PYTHONPATH=src python scripts/benchmark_execution.py
Legacy CLI/MCP defaults to 1 MiB; --entry-point runner defaults to 16 MiB.
Runner sizes may include 256 MiB; 1024 MiB additionally requires --allow-gib.
--entry-point redactor compares scalar/ASCII loops without SQLite operations.
All execution caches and response files are temporary. Tokenizer assets should
already be available locally. RSS excludes the observer and producer processes.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "tests/fixtures/execution/producer.py"
MIB = 1024 * 1024
RUNNER_QUOTA = 4 * 1024 * MIB
WARMUP_BYTES = 65536
REDACTOR_REFERENCE = ROOT / "tests/fixtures/execution/redactor_reference.py"


def peak_rss_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", w.DWORD),
                ("faults", w.DWORD),
                ("peak_working_set", ctypes.c_size_t),
                ("working_set", ctypes.c_size_t),
                ("peak_paged", ctypes.c_size_t),
                ("paged", ctypes.c_size_t),
                ("peak_nonpaged", ctypes.c_size_t),
                ("nonpaged", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t),
                ("peak_pagefile", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = w.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
        psapi.GetProcessMemoryInfo.restype = w.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return counters.peak_working_set
        return None
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


def rss_method():
    if os.name == "nt":
        return "GetProcessMemoryInfo.PeakWorkingSetSize (bytes)"
    if sys.platform == "darwin":
        return "getrusage(RUSAGE_SELF).ru_maxrss (bytes)"
    if sys.platform.startswith("linux"):
        return "getrusage(RUSAGE_SELF).ru_maxrss (KiB converted to bytes)"
    return "unavailable"


def _read_report(path):
    with path.open("rb") as stream:
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("Benchmark report exceeds its bound")
    return json.loads(payload)


def producer_worker(stats, size):
    """Same deterministic fixture as tests; peak RSS belongs only to this child."""
    spec = importlib.util.spec_from_file_location("execution_producer", PRODUCER)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    peak_rss_bytes()  # Warm the measurement API before output generation.
    # Warm the bounded generator allocation; never emit warm-up bytes to pipes.
    block = b"warmup\n" * 8192
    del block
    sys.argv = [str(PRODUCER), "--mode", "records", "--bytes", str(size)]
    code = 1
    try:
        code = producer.main()
        return code
    except BrokenPipeError:
        return 1
    finally:
        stats.write_text(
            json.dumps(
                {
                    "peak_rss_bytes": peak_rss_bytes(),
                    "rss_method": rss_method(),
                    "pid": os.getpid(),
                    "generated_bytes": size if code == 0 else None,
                }
            ),
            encoding="utf-8",
        )


def runner_worker(stats, size, stop_file, timeout, *, profile=False, filters=False):
    """Instrument M2 acknowledgements/finalization only inside this benchmark."""
    from dataclasses import asdict

    from usagetrim.core.cache import ContextCache
    from usagetrim.core.execution_contract import ExecutionMode, ExecutionRequest
    from usagetrim.core.execution_runner import CaptureLimits, run_execution

    directory = stats.parent
    os.environ["USAGETRIM_CACHE_MAX_BYTES"] = str(RUNNER_QUOTA)
    cache = ContextCache(directory / "cache.db")
    limits = CaptureLimits(quota_bytes=RUNNER_QUOTA)
    cancel, stop_watcher = threading.Event(), threading.Event()

    def watch():
        while not stop_watcher.wait(0.05):
            try:
                requested = stop_file.exists()
            except OSError:
                requested = True
            if requested:
                cancel.set()
                return

    watcher = threading.Thread(target=watch, name="benchmark-cancel", daemon=False)
    watcher.start()
    result = None
    instrumentation = None

    def request(byte_count, report):
        return ExecutionRequest(
            ExecutionMode.ARGV,
            argv=(
                sys.executable,
                str(Path(__file__).resolve()),
                "--_producer",
                "--_bytes",
                str(byte_count),
                "--_stats",
                str(report),
            ),
            timeout_seconds=timeout,
        )

    try:
        warm = run_execution(
            request(WARMUP_BYTES, directory / "warm-producer.json"),
            cache=cache,
            limits=limits,
            cancel_event=cancel,
        )
        if warm.returncode != 0 or not warm.capture.complete or warm.issues:
            raise RuntimeError("Runner warm-up did not complete")
        if filters:
            from usagetrim.core.bounded_filter import filter_execution

            filter_execution(warm, cache=cache, cancel_event=cancel)
        cache.clear()
        warm_peak = peak_rss_bytes()
        original_begin = cache.begin_artifact
        first_observation = None
        finalization = 0.0
        finish_first = finish_last = None

        def begin(*args, **kwargs):
            writer = original_begin(*args, **kwargs)
            observe, finish = writer._observe, writer.finish

            def observed(byte_count):
                nonlocal first_observation
                result = observe(byte_count)
                if first_observation is None:
                    first_observation = time.perf_counter() - started
                return result

            def finalized():
                nonlocal finalization, finish_first, finish_last
                before = time.perf_counter()
                if finish_first is None:
                    finish_first = before
                try:
                    return finish()
                finally:
                    finish_last = time.perf_counter()
                    finalization += finish_last - before

            writer._observe, writer.finish = observed, finalized
            return writer

        cache.begin_artifact = begin
        if profile:
            from execution_profile import ExecutionProfile

            instrumentation = ExecutionProfile(cache)
        else:
            instrumentation = nullcontext()
        with instrumentation:
            started = time.perf_counter()
            result = run_execution(
                request(size, directory / "producer.json"),
                cache=cache,
                limits=limits,
                cancel_event=cancel,
            )
            filtered = None
            filter_elapsed = 0.0
            if filters:
                from usagetrim.core.bounded_filter import filter_execution

                filter_started = time.perf_counter()
                filtered = filter_execution(result, cache=cache, cancel_event=cancel)
                result = filtered.result
                filter_elapsed = time.perf_counter() - filter_started
            wall = time.perf_counter() - started
        peak = peak_rss_bytes()  # Before any verification/report rendering.
        recovery = cache.execution_recovery(result.execution_id)
        retained = sum(item.stored_bytes for item in recovery.streams)
        chunks = sum(item.chunk_count for item in recovery.streams)
        retrieval_started = time.perf_counter()
        ranges = [
            cache.read_artifact(item.ref_id, first_line=2, last_line=5, max_bytes=4096)
            for item in recovery.streams
        ]
        retrieval = time.perf_counter() - retrieval_started
        producer_report = _read_report(directory / "producer.json")
        valid = (
            result.returncode == 0
            and result.capture.complete
            and not result.issues
            and peak is not None
            and producer_report["peak_rss_bytes"] is not None
            and producer_report["generated_bytes"] == size
            and retained == size
            and len(recovery.streams) == 2
            and all(item.complete and item.available for item in recovery.streams)
            and all(view.metadata.available and not view.response_truncated for view in ranges)
            and first_observation is not None
            and finish_first is not None
        )
        validation_errors = []
        if not valid:
            validation_errors.append("resource_measurement_validation_failed")
        report = {
            "entry_point": "runner",
            "measurement_valid": bool(valid),
            "wall_seconds": wall,
            "runner_elapsed_seconds": result.elapsed_seconds,
            "capture_throughput_mib_s": size / MIB / wall,
            "first_observation_seconds": first_observation,
            "first_observation_scope": "M2 reservation acknowledgement, not user delivery",
            "artifact_finalization_seconds": finalization,
            "artifact_finalization_wall_seconds": (
                finish_last - finish_first if finish_first is not None else None
            ),
            "generated_bytes": producer_report["generated_bytes"],
            "runner_peak_rss_bytes": peak,
            "warmup_peak_rss_bytes": warm_peak,
            "producer_peak_rss_bytes": producer_report["peak_rss_bytes"],
            "runner_pid": os.getpid(),
            "producer_pid": producer_report["pid"],
            "rss_method": rss_method(),
            "producer_rss_method": producer_report["rss_method"],
            "retained_bytes": retained,
            "stored_fragments": chunks,
            "retrieval_latency_seconds": retrieval,
            "retrieval_returned_bytes": sum(len(view.text.encode("utf-8")) for view in ranges),
            "retrieval_scope": "both streams, lines 2-5, max 4096 bytes each",
            "recovery_validation": "metadata, byte lengths and bounded ranges; not full hash verification",
            "termination_reason": result.termination_reason.value,
            "returncode": result.returncode,
            "capture_complete": result.capture.complete,
            "error_count": len(result.issues) + len(validation_errors),
            "errors": [issue.error_type for issue in result.issues] + validation_errors,
            "environment": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
            },
            "parameters": {
                "limits": asdict(limits),
                "warmup_bytes": WARMUP_BYTES,
                "timeout_seconds": timeout,
                "producer_sha256": hashlib.sha256(PRODUCER.read_bytes()).hexdigest(),
            },
        }
        if profile:
            report["profile"] = instrumentation.report()
            report["parameters"]["profile_enabled"] = True
        if filters:
            report["parameters"]["filters_enabled"] = True
            from usagetrim.core import bounded_filter

            report["parameters"]["filter_limits"] = {
                key: getattr(bounded_filter, key)
                for key in (
                    "SCAN_BYTES",
                    "SCAN_LINES",
                    "SMALL_TEXT_BYTES",
                    "OUTPUT_BYTES",
                    "SCAN_SECONDS",
                )
            }
            report["parameters"]["filter_source_sha256"] = hashlib.sha256(
                Path(bounded_filter.__file__).read_bytes()
            ).hexdigest()
            report["parameters"]["safe_filter_source_sha256"] = hashlib.sha256(
                Path(bounded_filter.safe_filter.__file__).read_bytes()
            ).hexdigest()
            report["filtering_seconds"] = filter_elapsed
            report["filter_views"] = [
                {"state": view.state, "coverage": view.coverage, "note": view.note}
                for view in filtered.views
            ]
            report["display_body_bytes"] = sum(
                len(view.text.encode("utf-8")) for view in filtered.views
            )
            report["first_bounded_progress_seconds"] = None
            report["progress_scope"] = (
                "not supported by CLI/MCP; first observation is storage acknowledgement only"
            )
            report["retained_disk_bytes"] = sum(
                path.stat().st_size
                for path in (
                    cache.db_path,
                    Path(str(cache.db_path) + "-wal"),
                    Path(str(cache.db_path) + "-shm"),
                )
                if path.exists()
            )
        stats.write_text(json.dumps(report), encoding="utf-8")
        return 0 if valid else 1
    except BaseException as error:
        # Never serialize captured output, arbitrary exceptions, or commands.
        partial = getattr(error, "result", None) or result
        failure = {
            "measurement_valid": False,
            "harness_error": type(error).__name__,
            "error_count": 1 + (len(partial.issues) if partial is not None else 0),
            "capture_complete": False,
            "returncode": partial.returncode if partial is not None else None,
            "termination_reason": partial.termination_reason.value if partial is not None else None,
        }
        if profile and instrumentation is not None:
            failure["profile"] = instrumentation.report()
        stats.write_text(json.dumps(failure), encoding="utf-8")
        return 1
    finally:
        cancel.set()
        stop_watcher.set()
        watcher.join(timeout=2)


def measure_runner(size, *, timeout=3600, profile=False, filters=False):
    """One fresh worker per size. Observer RSS is never reported as runner RSS."""
    if type(profile) is not bool:
        raise ValueError("profile must be a boolean")
    if type(filters) is not bool:
        raise ValueError("filters must be a boolean")
    extra = (["--profile"] if profile else []) + (["--filters"] if filters else [])
    return _measure_worker("runner", size, timeout, extra)


def measure_redactor(size, implementation, *, corpus="ascii", timeout=3600):
    """Use the same process isolation/cleanup as runner benchmarks, without CCR."""
    if implementation not in {"scalar", "ascii"} or corpus not in {"ascii", "mixed"}:
        raise ValueError("Unsupported redactor implementation or corpus")
    return _measure_worker(
        "redactor",
        size,
        timeout,
        ["--redactor-implementation", implementation, "--redactor-corpus", corpus],
    )


def _measure_worker(kind, size, timeout, extra_arguments):
    from usagetrim.core.execution_process import ProcessTree

    if type(size) is not int or not 0 < size <= 1024 * MIB:
        raise ValueError("Runner measurement size must be from 1 byte to 1 GiB")
    if type(timeout) not in {int, float} or not 0 < timeout <= 86400:
        raise ValueError("Measurement timeout must be finite, positive and at most 86400")
    with TemporaryDirectory(prefix=f"usagetrim-{kind}-resources-") as temporary:
        directory = Path(temporary)
        required_disk = size * 3 + 64 * MIB if kind == "runner" else 64 * MIB
        if shutil.disk_usage(directory).free < required_disk:
            raise RuntimeError("Insufficient temporary disk space for runner measurement")
        stats, stop = directory / "stats.json", directory / "cancel"
        tree = ProcessTree()
        process = None
        try:
            process = tree.start(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--_worker",
                    kind,
                    "--_bytes",
                    str(size),
                    "--_stats",
                    str(stats),
                    "--_stop-file",
                    str(stop),
                    "--timeout-seconds",
                    str(timeout),
                    *extra_arguments,
                ],
                shell=False,
                cwd=str(ROOT),
            )
            # Workers communicate only through bounded JSON files. Unintended
            # console writes fail instead of blocking an undrained observer pipe.
            process.stdout.close()
            process.stderr.close()
            deadline = time.monotonic() + 2 * timeout + 20  # Warm-up and measured invocation.
            gate = threading.Event()
            while tree.poll() is None:
                if time.monotonic() >= deadline:
                    stop.touch()
                    raise TimeoutError("Runner resource worker exceeded its deadline")
                gate.wait(0.05)
            report = _read_report(stats)
            report["worker_returncode"] = tree.poll()
            report["measurement_valid"] &= report["worker_returncode"] == 0
            return report
        finally:
            notification_failed = False
            try:
                stop.touch()
            except OSError:
                notification_failed = True
            process = tree.process
            try:
                if process is not None:
                    try:
                        # Allow existing cancellation to reap the producer.
                        end = time.monotonic() + 12
                        gate = threading.Event()
                        while (
                            not notification_failed
                            and tree.poll() is None
                            and time.monotonic() < end
                        ):
                            gate.wait(0.05)
                    finally:
                        tree.force()  # Only while the owned leader is waitable.
                        if tree.job is not None:
                            end = time.monotonic() + 5
                            while tree.alive() and time.monotonic() < end:
                                gate.wait(0.05)
                            if tree.alive():
                                raise RuntimeError("Benchmark job did not finish cleanup")
            finally:
                try:
                    tree.close()
                finally:
                    if process is not None:
                        process.wait(timeout=5)
            if notification_failed:
                raise RuntimeError("Benchmark cancellation notification failed") from None


def _redactor_parts(size, corpus):
    if corpus == "ascii":
        line = b"00000000 INFO execution fixture record=00000000".ljust(63) + b"\n"
        block = line * 64
    else:
        line = (
            "ASCII\tcolumns\nZażółć 日本 😀\n\x1b[31mcolored\x1b[0m\r\n"
            "\x1b]title\x07plain\nAuthorization: Bearer synthetic\npublic final line\n"
        ).encode()
        block = line * 128
    for offset in range(0, size, 4096):
        count = min(4096, size - offset)
        position = offset % len(block)
        part = block[position : position + count]
        if len(part) < count:
            part += block[: count - len(part)]
        yield part


def _exercise_redactor(redactor_type, size, corpus, *, stop_file, deadline):
    redactor = redactor_type()
    input_hash, output_hash = hashlib.sha256(), hashlib.sha256()
    input_bytes = output_bytes = records = 0
    next_check = 0.0
    for data in _redactor_parts(size, corpus):
        now = time.monotonic()
        if now >= next_check:
            if now >= deadline or stop_file.exists():
                raise TimeoutError("Redactor microbenchmark stopped")
            next_check = now + 0.05
        input_hash.update(data)
        input_bytes += len(data)
        for text in redactor.feed(data):
            encoded = text.encode("utf-8")
            output_hash.update(encoded)
            output_bytes += len(encoded)
            records += 1
    for text in redactor.finish():
        encoded = text.encode("utf-8")
        output_hash.update(encoded)
        output_bytes += len(encoded)
        records += 1
    return {
        "input_bytes": input_bytes,
        "input_sha256": input_hash.hexdigest(),
        "output_bytes": output_bytes,
        "output_sha256": output_hash.hexdigest(),
        "output_records": records,
        "redaction_incomplete": redactor.incomplete,
    }


def redactor_worker(stats, size, stop_file, timeout, implementation, corpus):
    """Measure the scalar loop or ASCII loop in a fresh process; no SQLite I/O."""
    from usagetrim.core import stream_redactor

    try:
        # Load both classes in both workers so code-loading footprint is shared.
        spec = importlib.util.spec_from_file_location("scalar_reference", REDACTOR_REFERENCE)
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        selected = {
            "scalar": reference.ScalarRedactor,
            "ascii": stream_redactor.StreamingRedactor,
        }[implementation]
        _exercise_redactor(
            selected,
            WARMUP_BYTES,
            corpus,
            stop_file=stop_file,
            deadline=time.monotonic() + timeout,
        )
        warm_peak = peak_rss_bytes()
        started = time.perf_counter()
        output = _exercise_redactor(
            selected,
            size,
            corpus,
            stop_file=stop_file,
            deadline=time.monotonic() + timeout,
        )
        elapsed = time.perf_counter() - started
        peak = peak_rss_bytes()
        report = {
            "entry_point": "redactor",
            "implementation": implementation,
            "corpus": corpus,
            "measurement_valid": peak is not None and output["input_bytes"] == size,
            "wall_seconds": elapsed,
            "throughput_mib_s": size / MIB / elapsed,
            "worker_peak_rss_bytes": peak,
            "warmup_peak_rss_bytes": warm_peak,
            "rss_method": rss_method(),
            "worker_pid": os.getpid(),
            "environment": {"platform": platform.platform(), "python": sys.version},
            "warmup_bytes": WARMUP_BYTES,
            "feed_bytes": 4096,
            "scalar_reference_sha256": hashlib.sha256(REDACTOR_REFERENCE.read_bytes()).hexdigest(),
            "redactor_source_sha256": hashlib.sha256(
                Path(stream_redactor.__file__).read_bytes()
            ).hexdigest(),
            "scope": "decode, redaction, assembly and incremental hashes; no SQLite operations",
            **output,
        }
        stats.write_text(json.dumps(report), encoding="utf-8")
        return 0 if report["measurement_valid"] else 1
    except BaseException as error:
        stats.write_text(
            json.dumps({"measurement_valid": False, "harness_error": type(error).__name__}),
            encoding="utf-8",
        )
        return 1


def redactor_comparison(results):
    by_kind = {row.get("implementation"): row for row in results}
    before, after = by_kind.get("scalar"), by_kind.get("ascii")
    comparable = bool(
        before
        and after
        and before.get("measurement_valid")
        and after.get("measurement_valid")
        and all(
            before.get(key) == after.get(key)
            for key in (
                "input_bytes",
                "input_sha256",
                "corpus",
                "feed_bytes",
                "warmup_bytes",
                "rss_method",
                "scalar_reference_sha256",
                "redactor_source_sha256",
                "environment",
            )
        )
        and before.get("wall_seconds", 0) > 0
        and after.get("wall_seconds", 0) > 0
        and type(before.get("worker_peak_rss_bytes")) is int
        and type(after.get("worker_peak_rss_bytes")) is int
    )
    equivalent = (
        all(
            before.get(key) == after.get(key)
            for key in ("output_bytes", "output_sha256", "output_records", "redaction_incomplete")
        )
        if comparable
        else None
    )
    return {
        "comparable": comparable,
        "equivalent_output": equivalent,
        "scalar_over_ascii_wall_ratio": (
            before["wall_seconds"] / after["wall_seconds"] if comparable and equivalent else None
        ),
        "ascii_minus_scalar_peak_rss_bytes": (
            after["worker_peak_rss_bytes"] - before["worker_peak_rss_bytes"]
            if comparable and equivalent
            else None
        ),
    }


def rss_growth(results):
    """Informational target only; missing/incomparable samples never count as PASS."""
    samples = [row for row in results if row.get("measurement_valid")]
    small_rows = [row for row in samples if row.get("generated_bytes") == 16 * MIB]
    large_rows = [row for row in samples if row.get("generated_bytes") == 1024 * MIB]
    small = small_rows[0] if len(small_rows) == 1 else None
    large = large_rows[0] if len(large_rows) == 1 else None
    comparable = bool(
        small
        and large
        and small.get("rss_method")
        and small.get("rss_method") == large.get("rss_method")
        and small.get("parameters")
        and small.get("parameters") == large.get("parameters")
        and small.get("environment")
        and small.get("environment") == large.get("environment")
        and type(small.get("runner_peak_rss_bytes")) is int
        and type(large.get("runner_peak_rss_bytes")) is int
        and small["runner_peak_rss_bytes"] > 0
        and large["runner_peak_rss_bytes"] > 0
    )
    delta = large["runner_peak_rss_bytes"] - small["runner_peak_rss_bytes"] if comparable else None
    return {
        "measured": comparable,
        "delta_bytes": delta,
        "threshold_bytes": 64 * MIB,
        "within_64_mib": delta <= 64 * MIB if comparable else None,
    }


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
    sizes = parser.add_mutually_exclusive_group()
    sizes.add_argument("--size-mib", type=int, choices=(1, 16, 256, 1024))
    sizes.add_argument("--sizes-mib", type=int, nargs="+", choices=(1, 16, 256, 1024))
    parser.add_argument(
        "--allow-gib", action="store_true", help="Explicitly permit a 1 GiB resource sample"
    )
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Aggregate runner stage timings in the isolated worker",
    )
    parser.add_argument(
        "--filters",
        action="store_true",
        help="Include M6 bounded display filtering in runner measurement",
    )
    parser.add_argument(
        "--quality",
        action="store_true",
        help="Replay the existing fixture corpus through bounded and small-input legacy safe filters",
    )
    parser.add_argument(
        "--entry-point", choices=("cli", "mcp", "both", "runner", "redactor"), default="both"
    )
    parser.add_argument(
        "--redactor-implementation", choices=("both", "scalar", "ascii"), default="both"
    )
    parser.add_argument("--redactor-corpus", choices=("ascii", "mixed"), default="ascii")
    parser.add_argument(
        "--_worker", choices=("cli", "mcp", "runner", "redactor"), help=argparse.SUPPRESS
    )
    parser.add_argument("--_stats", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_producer", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_bytes", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--_stop-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.quality:
        from usagetrim.core.benchmark import benchmark_bounded_case
        from usagetrim.core.benchmark_corpus import load_benchmark_cases

        if args._worker or args._producer or args.size_mib or args.sizes_mib:
            parser.error("--quality is a separate small-fixture report, not a resource profile")
        cases = load_benchmark_cases(ROOT / "tests/fixtures/benchmarks/manifest.json")
        rows = [benchmark_bounded_case(case) for case in cases]
        print(
            json.dumps(
                {
                    "scope": "offline fixture replay; byte sizes, not provider tokens",
                    "results": rows,
                    "rollout": "opt-in; native resource/cross-platform gates must be measured separately",
                    "rss_growth_measured": False,
                },
                indent=2,
            )
        )
        return
    if args.filters and args.entry_point != "runner" and args._worker != "runner":
        parser.error("--filters requires --entry-point runner")
    if args.profile and args.entry_point != "runner" and args._worker != "runner":
        parser.error("--profile requires --entry-point runner")
    if not 0 < args.timeout_seconds <= 86400:
        parser.error("timeout-seconds must be finite, positive and at most 86400")
    size = args._bytes if args._bytes is not None else (args.size_mib or 1) * MIB
    if args._producer:
        if args._stats is None or not 0 < size <= 1024 * MIB:
            parser.error("producer requires stats and a bounded size")
        raise SystemExit(producer_worker(args._stats, size))
    if args._worker:
        if args._stats is None:
            parser.error("worker requires stats path")
        if args._worker == "runner":
            if args._stop_file is None or not 0 < size <= 1024 * MIB:
                parser.error("runner worker requires a stop file and bounded size")
            raise SystemExit(
                runner_worker(
                    args._stats,
                    size,
                    args._stop_file,
                    args.timeout_seconds,
                    profile=args.profile,
                    filters=args.filters,
                )
            )
        if args._worker == "redactor":
            if (
                args._stop_file is None
                or not 0 < size <= 1024 * MIB
                or args.redactor_implementation == "both"
            ):
                parser.error(
                    "redactor worker requires a stop file, bounded size and concrete implementation"
                )
            raise SystemExit(
                redactor_worker(
                    args._stats,
                    size,
                    args._stop_file,
                    args.timeout_seconds,
                    args.redactor_implementation,
                    args.redactor_corpus,
                )
            )
        worker(args._worker, args._stats, size)
        return
    if args.entry_point == "redactor":
        if args.sizes_mib is not None:
            parser.error("redactor microbenchmark accepts one --size-mib per comparison")
        count = args.size_mib or 16
        if count == 1024 and not args.allow_gib:
            parser.error("1 GiB requires explicit --allow-gib")
        implementations = (
            ("scalar", "ascii")
            if args.redactor_implementation == "both"
            else (args.redactor_implementation,)
        )
        results = [
            measure_redactor(
                count * MIB, kind, corpus=args.redactor_corpus, timeout=args.timeout_seconds
            )
            for kind in implementations
        ]
        comparison = redactor_comparison(results)
        print(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "python": sys.version,
                    "results": results,
                    "comparison": comparison,
                    "note": "isolated redactor only; not end-to-end capture throughput",
                },
                indent=2,
            )
        )
        if not all(row["measurement_valid"] for row in results) or (
            len(implementations) == 2 and not comparison["equivalent_output"]
        ):
            raise SystemExit(1)
        return
    if args.entry_point == "runner":
        selected = args.sizes_mib or [args.size_mib or 16]
        if len(set(selected)) != len(selected):
            parser.error("sizes must be unique; repeat the command for independent batches")
        if 1024 in selected and not args.allow_gib:
            parser.error("1 GiB requires explicit --allow-gib")
        results = [
            measure_runner(
                count * MIB,
                timeout=args.timeout_seconds,
                profile=args.profile,
                filters=args.filters,
            )
            for count in selected
        ]
        print(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "python": sys.version,
                    "observer_pid": os.getpid(),
                    "machine": platform.machine(),
                    "sqlite": sqlite3.sqlite_version,
                    "producer_sha256": hashlib.sha256(PRODUCER.read_bytes()).hexdigest(),
                    "results": results,
                    "rss_growth_target": rss_growth(results),
                    "rss_scope": "self high-water at operation completion; observer excluded; never a sum",
                },
                indent=2,
            )
        )
        if not all(row["measurement_valid"] for row in results):
            raise SystemExit(1)
        return
    if args.sizes_mib is not None or args.size_mib not in {None, 1, 16}:
        parser.error("multiple/large sizes require --entry-point runner")
    entries = ("cli", "mcp") if args.entry_point == "both" else (args.entry_point,)
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
                "rss_method": rss_method(),
                "recovery_latency_scope": "ContextCache construction and full retrieve",
                "results": [measure(entry, size) for entry in entries],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
