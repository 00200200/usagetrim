# Internal bounded command capture and shutdown (M3A–M3B)

`core.execution_runner.run_execution` is an internal prototype, not a replacement
for CLI `run` or MCP `usagetrim_exec`. Neither entry point uses it. Timeout,
per-execution cancellation and owned-tree shutdown are implemented internally;
native platform verification and the remaining M3 work precede public integration.

## API

```python
from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_contract import ExecutionMode, ExecutionRequest
from threading import Event
from usagetrim.core.execution_runner import CaptureLimits, ShutdownPolicy, run_execution

cancel = Event()  # Owned by this invocation; another thread may call cancel.set().
result = run_execution(
    ExecutionRequest(
        ExecutionMode.ARGV,
        argv=("tool", "space name", ""),
        timeout_seconds=30,
    ),
    cache=ContextCache(),
    limits=CaptureLimits(chunk_bytes=8192, queue_chunks=8, preview_bytes=4096),
    cancel_event=cancel,
    shutdown=ShutdownPolicy(graceful_seconds=2, force_seconds=2, reader_join_seconds=2),
)
for stream in result.streams:
    consume_preview(stream.stream, stream.capture.text)
    # RecoveryInfo distinguishes availability from completeness.
    inspect_recovery(stream.recovery)
```

ARGV is passed directly with `shell=False`; it is never joined or reparsed.
SHELL passes the opaque command to the platform's ordinary `Popen(shell=True)`
shell. `cwd` is preserved; stdin and environment are inherited. No PTY is used.
The shell's exit status is the observed child status in shell mode.
Process isolation is not PTY emulation: a new POSIX session has no controlling
terminal, even though stdin is inherited. Programs requiring `/dev/tty` or shell
job control need separate integration policy; this runner is not yet a public
interactive-command replacement.

M1 receives only additive fields: `ExecutionResult.execution_id`, `streams`, and
`StreamCapture`. Existing merged representations/defaults remain unchanged.
The runner returns a separate-stream representation with **no merged text** in
the root `capture`. Each stream has a bounded preview and its own recovery ref.
The aggregate `recovery` remains unset. Preview truncation is independent from
storage completeness; `replacement_occurred` remains unknown, following M2.
All captured text, refs, commands and execution IDs remain hidden from `repr`.

## Bounds and ownership

Two non-daemon reader threads perform interruptible, unbuffered binary reads. A single calling
thread collects a FIFO queue and writes to CCR. Readers start waiting on a gate
before spawning the child, so a failure to start either reader cannot leave a
child with an undrained pipe. The gate opens only once both pipes are assigned.
On POSIX, nonblocking descriptors and selectors impose a 50 ms polling interval.
On Windows, `PeekNamedPipe` checks availability before a bounded read; no blocking
read waits for nonexistent data. Queue puts also poll a stop event. Both readers
close their own pipe and publish a terminal event, including on read failure.
During shutdown terminal messages may be omitted when the queue is full: the
collector also inspects the reader's final EOF/failure state. No new thread is
needed for cancellation. Joins share one finite deadline.

The queue holds at most `queue_chunks` events, each with at most `chunk_bytes`
raw bytes. Full queues apply backpressure to both readers and eventually the
child. In addition to queued payloads, at most two readers and one collector
retain a payload each: approximately `(queue_chunks + 3) * chunk_bytes` raw
bytes (88 KiB with defaults), plus bounded object/allocator overhead and OS pipe
buffers. This is a payload bound, not a total process RSS guarantee.

`chunk_bytes` is 1–65,536; `queue_chunks` is 1–64. Each M2 writer additionally
uses its existing bounded UTF-8 decoder, 16 KiB line quarantine, 4 KiB output
buffer, bounded redaction scratch state and SQLite state. Two returned previews
use at most `2 * preview_bytes` UTF-8 bytes; each preview is 1–65,536 bytes.
Diagnostics retain fixed-category issues per phase/source (with distinct cleanup
failure categories retained separately). No reader
accumulates lines or results. Neither `communicate()` nor whole-output reads,
merging, decoding or reconstruction are used.

`quota_bytes`, when supplied, is M2's shared CCR accounting budget, **not** an
independent per-stream allowance. Existing active-writer limits also apply.
Idle writers receive lease heartbeats without extra threads. A store operation
that itself hangs is not forcibly interrupted by this runner.

## CCR and failures

Stdout and stderr use separate `ArtifactWriter` instances, sharing one random
execution ID. Their decoders and redactors are independent. The collector's
queue acceptance order becomes M2 observation order. This is not exact child
emission chronology, and sanitized chunks must not be interleaved to invent it.

M2 sanitizes before persistence. ANSI/OSC normalization, replacement decoding,
credential-line masking and fail-closed oversized-line behavior are unchanged.
A very long line can make the redacted view incomplete even though every pipe
byte was drained. There is no raw fallback, including after storage failure.

Initialization, append, heartbeat, finish, abort and recovery lookup failures
produce bounded categories without exception bodies, paths, commands or raw
output. An affected writer is aborted; subsequent source bytes are discarded
while both pipes continue draining. Other streams may still complete. If the
abort cannot be persisted, M2's writing state/lease remains incomplete rather
than claiming completion. No retries spill unsanitized data elsewhere.

After natural exit, `finish()` publishes each healthy stream. Timeout and
cancellation use `abort()`, preserving sanitized complete lines but discarding
quarantined partial lines and decoder state. Their streams are never advertised
as complete. A storage failure during abort remains an issue; the persisted state
may remain writing until M2's lease recovery marks it incomplete. Recovery metadata
is resolved using `execution_recovery()`; previews use only `read_artifact()`
with a response cap. Missing, corrupt or unreadable recovery cannot establish
complete capture. Read failure leaves recovery availability unknown. A preview
does not verify the unread remainder of an artifact or pin it against eviction;
availability is a point-in-time observation, subject to subsequent eviction.

Child outcome is independent: exit zero can coexist with capture/storage issues,
and nonzero exit can coexist with complete capture. Failure to spawn has no
child returncode. Negative observed returncodes mean signals only on POSIX.
Elapsed time includes capture/storage and process cleanup, excluding the
final bounded recovery preview reads.

## Deadlines, cancellation and Ctrl-C

A monotonic deadline starts on entry, including storage setup and pipe capture.
It is checked before launching and on every collector iteration, even under
continuous stdout/stderr. It still expires if the direct child exited but a
descendant retains stdout/stderr. A timeout reports `TIMED_OUT`, independent of
the actual child returncode (which may already be zero). Unknown returncodes stay
`None`; no timeout/cancellation code is fabricated.

`cancel_event` is optional, thread-safe and specific to one invocation. An event
already set on entry returns `CANCELLED` without cache/process side effects.
An event set during storage setup is observed before launch. In-flight requests
drain during shutdown and retain partial recovery. Cancellation and timeout keep
the first observed cause; simultaneous checks prefer explicit cancellation.
Sharing an event across calls intentionally cancels all those calls; use separate
events to isolate MCP requests in a future adapter.

No signal handler is installed or replaced. A Python `KeyboardInterrupt` during
execution initiates cancellation and cleanup, then raises `ExecutionInterrupted`,
a `KeyboardInterrupt` subclass. Its `.result` contains the partial `ExecutionResult`
and any cleanup failures. No command, output or exception body appears in its
message. This does not intercept signals in a non-main Python thread; use the
event API from another thread instead. Native process creation is not preemptible;
Python/OS failures before `Popen` transfers child ownership remain a startup
limitation, rather than a hard real-time cancellation guarantee.

## Owned process trees

POSIX launches a fresh session/process group (`start_new_session=True`). Shutdown
sends SIGTERM only to that group, allows a bounded grace period, then sends
SIGKILL. Status inspection uses `waitid(WNOWAIT)` so the waitable group leader's
PID stays reserved until the last group signal; only then does `Popen.wait()`
reap it. Linux retains Python's `os.waitid`/`WNOWAIT` path. On macOS Python
3.11–3.12, or when Python does not expose either symbol, a separate ctypes adapter
calls Darwin's system `waitid` with Darwin's own flags and 64-bit `siginfo_t` ABI.
It uses only local `/usr/lib/libSystem.B.dylib`, checks the ABI before spawning,
and never falls back to reaping `Popen.poll()`. A missing system function or an
unsupported ABI refuses startup. See Apple's definitions of
[`waitid` options](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/wait.h)
and [`siginfo_t`](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/signal.h).
Callers must not install a reaper
which consumes the runner's children. A detected loss of waitability refuses
group signaling and reports a cleanup failure rather than accepting a guessed
exit code or risking a reused process-group ID.

On Darwin, `killpg` may return `EPERM` when only unreaped zombies remain. That
error is dismissed only while the exited leader is still verified as waitable
and a read-only local `libproc` probe confirms that every listed group member is
a zombie. Membership is enumerated again after status checks to detect a child
forked before its parent exited. This uses public `proc_listpids` and
`proc_pidinfo(PROC_PIDT_SHORTBSDINFO, arg=1)` data, including zombies, not `ps`,
a shell, or a reaping wait. The short form retains the process-group identifier
even when session information is no longer present. See Apple's
[process inspection implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/proc_info.c)
and [public structures](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h).
The adapter checks the 64-bit ABI and caps enumeration at 4096 members. Missing
symbols, denied/truncated reads, a missing leader or changing membership leave
the original permission error visible as a cleanup failure. A live descendant
never qualifies for this exception. Linux signaling and Windows jobs are
unchanged; actual Darwin behavior still requires native macOS CI/manual tests.

Windows creates an unnamed Job Object with `KILL_ON_JOB_CLOSE`. `Popen` creates
the child suspended in a new console process group. The job is assigned before
resuming its sole initial thread using documented Toolhelp/OpenThread/ResumeThread
APIs. Assignment/resume failure is a controlled failed start: the suspended child
is terminated, never silently launched outside the job. Descendants normally
inherit membership; breakaway permission is not enabled. Nested host jobs or
restricted process rights can prevent startup and are reported, not bypassed.
Graceful shutdown attempts CTRL_BREAK for the owned console group. No-console
hosts and applications without a compatible handler may reject or ignore it;
that failure is visible, and `TerminateJobObject` performs forced tree shutdown.
Closing the owned job is a final kill-on-close containment barrier.

When a command exits naturally and both pipes reach EOF, the execution scope is
closed immediately: any remaining owned background descendants are forcibly
terminated before releasing ownership. This does not change the already observed
direct-child exit status or the captured streams. Explicit timeout/cancellation
instead always uses the bounded graceful/forced phases.

## Bounds and remaining limitations

The graceful and forced phases default to two seconds each. At the forced-phase
deadline, pipe readers are explicitly stopped even if EOF never arrives; bounded
queue puts cannot strand them. Reader joins share a two-second budget; direct
child reaping also has a finite two-second budget. Windows job close retries
interrupted closes for at most the configured forced phase. No shutdown path
uses an unbounded `wait()` or `join()`. These phase budgets are additional cleanup
time after the command deadline, not additional permitted execution time.

Synchronous CCR/SQLite calls, OS process creation, and kernel/native calls are not
forcibly preempted. SQLite's existing busy timeout remains in use, but disk/kernel
stalls or custom adapters can delay observation of a deadline. The runner is not
a hard real-time system. A normal call with no timeout and no cancellation still
waits for natural exit and EOF; specify a timeout to bound descendant-held pipes.

POSIX groups are not a security sandbox: a deliberately detached descendant can
escape the group. The runner stops its readers rather than waiting forever for
such a process's pipe, reports incomplete capture/unverified cleanup when observed,
and does not kill unrelated processes. Permission errors, failed signals, failed
joins and unverified tree exit are exposed as `ExecutionIssue(phase="cleanup", ...)`.
Unexpected status/API exceptions also refuse unverified signals. Diagnostic
failure cannot skip independent pipe shutdown, job close or bounded direct-child
reaping. Partially initialized pipe handles are treated as unavailable capture,
not as EOF or a complete empty stream. Only previously observed real returncodes
may replace an unavailable final status; a guessed ECHILD-derived zero is rejected.
An uninterruptible kernel task may outlive SIGKILL; Python threads cannot safely
be killed if a native call itself fails to return. No full cleanup is claimed in
these failure cases. A remaining POSIX group may consist only of reparented zombies;
the runner cannot portably prove their state and reports exit as unverified.

Windows and macOS require native test execution; off-platform code inspection
and Linux tests are not proof of compatibility. CLI/MCP wiring and filter
integration remain outside M3. Resource validation and additional regression
coverage are provided below; no empirical resource result is implied by their existence.

## Resource validation (M3C)

The existing `scripts/benchmark_execution.py` now has an internal-runner mode.
The legacy CLI/MCP baseline remains available with its previous 1 MiB default.
Runner measurements default to 16 MiB; 256 MiB is a separate manual variant.
1 GiB requires an explicit size selection **and** `--allow-gib`.

```bash
PYTHONPATH=src python scripts/benchmark_execution.py --entry-point runner --size-mib 16
PYTHONPATH=src python scripts/benchmark_execution.py --entry-point runner --size-mib 256
PYTHONPATH=src python scripts/benchmark_execution.py --entry-point runner --sizes-mib 16 256 1024 --allow-gib > runner-resources.json
```

Large profiles have **not been measured as part of this implementation**. In
particular, `peak RSS(1 GiB) - peak RSS(16 MiB) <= 64 MiB` is an unverified target,
not a claim. JSON reports `measured=false` / `within_64_mib=null` if either sample
is absent, invalid, unmeasured or has a different method, environment or configuration.
The target is informational and does not impose a CI performance gate. Repeating
the same command is recommended; one pair is not a proof of all-size memory bounds.

Each size runs in its own interpreter. That worker warms the actual runner/M2
path with 64 KiB, clears warm-up artifacts, and measures the requested invocation
using the same 8 KiB reads, eight queue slots, 4 KiB previews and 4 GiB logical
CCR budget. The deterministic producer uses the existing fixture's `records`
mode, alternating 64 KiB stdout/stderr blocks, with no shell or full-log buffer.
The producer warms a fixed small allocation and its RSS measurement API too.
All profiles use identical settings, including timeout; the producer SHA-256 is
included. Timeout defaults to 3600 seconds and may be set explicitly for slow disks.

RSS belongs to **one process at a time**:

| Platform | Measurement | Normalization |
| --- | --- | --- |
| Linux | `getrusage(RUSAGE_SELF).ru_maxrss` | KiB converted to bytes |
| macOS | `getrusage(RUSAGE_SELF).ru_maxrss` | Native bytes |
| Windows | `GetProcessMemoryInfo.PeakWorkingSetSize` | Native bytes |

The runner worker reports its own high-water after `run_execution()` returns,
before report rendering and extra retrieval verification. Its peak includes imports,
warm-up and instrumentation: high-water is not reset or subtracted. The producer
separately reports its own high-water after output generation. These are cumulative
self high-water snapshots at operation completion, not final interpreter-shutdown
measurements or private-heap measurements. Producer, runner and observer PIDs are
distinct; neither `RUSAGE_CHILDREN` nor RSS summation is used. The observer's memory
is excluded. Compare same-platform, same-environment batches; working-set and Unix
RSS accounting, startup/exec accounting, allocator and filesystem cache differences
make cross-platform performance rankings inappropriate.

JSON also reports total `run_execution()` wall time, generated MiB divided by
that wall time, first M2 observation reservation acknowledgement, artifact finish
time (sum and wall interval), retained bytes, chunk count, bounded-range retrieval
latency, actual exit status, capture completeness, and error categories/count.
First observation is measured by a benchmark-only wrapper around M2 `_observe`;
it is **not** time to user-visible delivery. Finalization is instrumented around
the actual writers' `finish`, without changing the runner's public contract.
The producer independently confirms bytes generated; retention must match them
and both artifacts must be complete for the measurement to be valid.

Retrieval reads only lines 2–5 with a 4 KiB cap per stream. It does not reconstruct
the large artifact or claim a full aggregate hash verification. Full ordered,
decoded/redacted recovery hashes are covered separately by small regression tests.
This is a warm prefix-range lookup, not a cold random-seek or whole-file throughput test.
Missing/evicted snapshots, failed finalization, or truncated storage never count
as a successful resource measurement.

Each sample uses a temporary cache and bounded JSON reports (64 KiB), deleted
after measurement. Allow at least roughly three times the generated size plus
64 MiB of free temporary disk space. The preflight is an estimate, not a reservation;
SQLite pages, indices, journals and concurrent disk use can still exhaust space.
On observer failure a cancellation file asks the worker to clean up; subsequent
cleanup uses the existing owned-tree machinery with bounded waits. Windows nested
jobs contain the worker and producer. On POSIX the producer is the known synchronous
fixture only: closing a forcibly stopped worker's pipe readers makes blocked
producer writes fail and exit. This cleanup strategy must not be generalized to
arbitrary commands or detached processes. Kernel/SQLite I/O stalls retain the
runner's existing limitations; cleanup failures invalidate the measurement.

## CI coverage

Ubuntu keeps the full Python 3.11–3.13 test matrix. The same workflow adds focused
execution tests on macOS and Windows for all three Python versions, with no
`continue-on-error`. Native Windows Job Object tests run on Windows; native Darwin
adapter tests run on macOS, explicitly removing Python wait API symbols even on
Python 3.13. POSIX group tests run where supported. This schedules native validation;
it is not evidence that these jobs have already passed.

Only a 64 KiB resource-harness smoke test runs in ordinary pytest/CI. There are no
speed/RSS-delta thresholds in that test and no automatic 16 MiB, 256 MiB or 1 GiB
resource profile. Fault tests cover SQLite busy/locked, I/O/full-disk simulation,
permissions, finalization and reader errors, saturation timeouts, queue-full
cancellation, parallel executions and cleanup failures. Filesystem full/permission
faults are simulated, not destructive changes to the CI host's filesystem.

## Optional aggregate throughput profile

```bash
PYTHONPATH=src python scripts/benchmark_execution.py --entry-point runner --size-mib 16 --profile > runner-profile-16.json
```

Profiling is off by default. Ordinary benchmark JSON is unchanged; only profiled
samples gain `results[].profile` and `parameters.profile_enabled=true`. Profiled
and unprofiled samples are deliberately not comparable in the RSS target check.
No throughput improvement has been measured or claimed by adding this profiler.

Instrumentation is confined to the isolated benchmark worker, after warm-up,
and restored before extra verification/retrieval. It does not modify M2 files,
schema, SQLite pragmas, transaction boundaries or durability. A public SQLite
Connection subclass measures the same calls made by M2. A read-only settings
snapshot records journal mode, synchronous level, page/cache size, busy timeout
and foreign-key setting on a connection configured by M2.

Only aggregate counts, bytes, errors and monotonic wall durations are kept for
fixed stage names. No SQL/bindings, output, event timestamps, commands or exception
messages are recorded. Storage does not grow with fragment/event count. Timers
run per append/flush/SQL call and raw pipe read, **not per character or output line**.
They still add overhead: use the profile to locate costs, then repeat an ordinary
benchmark to assess any subsequently approved optimization.
Stages which were never observed are omitted rather than synthesized as events.

Interpret `results[].profile.stages` as follows:

| Stage | Interpretation |
| --- | --- |
| `writer.append` | Collector's inclusive storage/redaction time and actual input bytes/calls |
| `writer.observe` | Observation reservation, including its transaction |
| `writer.flush` / `writer.flush_committed` | All flush attempts / successful payload commits; empty flush attempts are included only in the former |
| `sqlite.connect`, `sqlite.execute.*`, `sqlite.commit`, `sqlite.close` | Connection setup, statement classes, commit and close costs |
| `quota.reserve`, `quota.usage` | Reservation and constant-row quota lookup costs |
| `redaction_and_assembly_residual` | `_accept` time minus nested flush time: decoding/redaction, regexes, UTF-8 encoding and buffer assembly, not pure regex CPU time |
| `pipe.poll_read.stdout/stderr` | Readiness waiting plus bounded raw reads; excludes queue-put waits |
| `queue.put` / `queue.full_timeouts` | Reader enqueue time and timed-out full-queue attempts, indicating backpressure |
| `queue.get` / `queue.empty_timeouts` | Collector waiting for input |
| `process.start/poll/close`, `runner.reader_shutdown`, `recovery.preview` | Launch, process checks, shutdown and final bounded previews inside the runner |

Do **not** add these times together. Writer stages nest; `sqlite.execute` includes
trigger execution, without counting trigger statements separately. Cursor fetch
time is not separately timed. Reader and queue durations overlap the collector
and each other, and may sum to more than total wall time. Error counters count
failed calls at each level, so one exception can appear at several nested stages;
queue full/empty timeouts are expected counters, not transport errors. The
redaction residual assumes the benchmark's single collector, not concurrent M2
writers in one profiled process.

### Static cost model and pending M2 proposals

The current implementation publishes each 4 KiB safe payload chunk in a separate
SQLite transaction. Each raw `append` first commits a separate observation
reservation. Every such transaction opens/closes a connection and configures its
pragmas. With default reads of at most 8 KiB, successful ASCII capture implies:

| Output | Payload commits | Minimum observation commits | Minimum combined write transactions |
| --- | ---: | ---: | ---: |
| 16 MiB | 4096 | 2048 | 6144 |
| 256 MiB | 65536 | 32768 | 98304 |

This excludes schema/startup, leases and finalization. Short reads increase the
observation count; use `writer.append.bytes / calls` to see actual average input
size. This is a code-derived lower bound, **not** a measured commit/fsync latency.
The quota hot path reads a transactional counter row rather than rescanning CCR;
eviction scans are only entered when reservation exceeds the configured budget.
The benchmark's fixed 4 GiB budget should avoid eviction for these samples.

Many short durable transactions and connection lifetimes are a strong candidate,
but the bounded redactor also processes every character and runs detectors per
line. Neither is proven dominant without the profile. The harness does not repeat
capture or fully retrieve the measured log; extra verification is outside its wall
timer. High enqueue time/full queues alongside high append time normally reflect
storage backpressure, rather than a reason to increase the queue.
The 50 ms pipe/queue polling limit is not an unconditional per-fragment sleep:
ready selectors and queues wake immediately. Removing cancellation polling is
not a justified throughput fix.

Potential work belongs in the separate M2 review (PR #151), not an unreviewed
runner/harness shortcut:

1. If connection/pragma costs dominate, investigate reuse **within one append**
   while retaining each existing transaction/commit boundary. Bound lifetime and
   preserve thread affinity, rollback, quota checks and cleanup. A persistent
   per-writer connection would need particular care because current callers may
   invoke a writer sequentially from different threads.
2. If commits dominate, propose bounded transaction batching while keeping the
   existing 4 KiB chunk layout/checksums. Observation-before-redaction durability
   and intermediate committed prefixes are existing guarantees: batching changes
   crash/partial-failure semantics unless carefully designed and agreed. It needs
   separate quota, ownership, interruption and recovery tests.
3. If the residual dominates, profile/redesign safe redaction/assembly in M2,
   preserving all boundary and control-sequence cases. No detector is disabled.
4. Larger bounded reads can reduce observations only if the OS supplies larger
   reads; they cannot remove payload-commit costs. This is an experiment with a
   different configuration, not a justified default change yet.

Increasing fragment size casually would change reader/format bounds. Enlarging
queues only stores more pending data. Disabling synchronous writes, foreign keys
or integrity checks is not an acceptable optimization. No M2 optimization above
has been applied or benchmarked as part of the initial profiling change.

### Isolated redactor comparison

A subsequent narrow optimization groups printable ASCII and TAB into runs in
`StreamingRedactor._consume()`. It removes repeated Python dispatch, UTF-8
encoding and list appends for those characters. Other Unicode and all terminal
controls still use the scalar state machine. A pending CR must be resolved by
that state machine before the fast path can resume.

Runs remain quarantined until `_record()` performs the existing whole-line
credential/PEM checks. Nothing is published early. The 16 KiB line-byte limit,
terminal-control bound, incremental UTF-8 decoding and fail-closed policy are
unchanged. The in-memory line list now holds runs as well as individual
characters; artifact bytes, observation spans, schema and M2 APIs are unchanged.
This change is prepared in the M3 worktree for review; its eventual placement in
the separate M2 PR must be decided after validation.

The measured redaction/assembly residual includes decoding, detectors, record
assembly and UTF-8 encoding as well as `_consume()`. It is not evidence that all
of that time can be removed by optimizing the character loop.

Use the existing harness's optional redactor mode to compare the frozen scalar
loop with the optimized loop, without SQLite operations:

```bash
PYTHONPATH=src python scripts/benchmark_execution.py --entry-point redactor --size-mib 16
PYTHONPATH=src python scripts/benchmark_execution.py --entry-point redactor --size-mib 16 --redactor-corpus mixed
```

The default is both implementations, each in its own isolated process with a
64 KiB warm-up, identical 4 KiB feeds and the existing bounded observer/cleanup
mechanism. `--redactor-implementation scalar` or `ascii` selects one. Both
workers load both classes; the scalar fixture freezes only the old consumption
loop and intentionally shares the other current policy helpers. It is a
differential reference, not an independent specification of safe redaction.

Reports include wall time, throughput, process peak RSS, warm-up peak RSS,
platform/Python metadata, source hashes and incremental input/output hashes.
The existing RSS methods are reused: self high-water RSS on Linux/macOS, peak
working set on Windows. Values include interpreter and imports, exclude the
observer, and should not be compared across different platforms/RSS methods.
The process high-water mark includes warm-up; it cannot be reset between phases.

Input generation and hashing use bounded buffers and never retain the full
output. The mixed corpus exercises Unicode, ANSI/OSC, CRLF and synthetic
credential labels. Its requested byte count may end inside a control or UTF-8
sequence; the resulting completeness flag is reported and compared, not assumed
to be true. The comparison emits a time ratio/RSS difference only when inputs,
configuration, source hashes and output hashes/record counts/completeness agree.
Hashing and generation are included in both times. No raw payload is reported.

This is not an end-to-end capture throughput measurement. ASCII-heavy inputs
are the intended fast case; Unicode/control-heavy inputs and tiny feeds may
benefit less or incur dispatch overhead. No timings or improvement claims are
recorded here because these comparisons have not been run.

Differential and integration checks to run manually:

```bash
PYTHONPATH=src pytest tests/test_stream_redactor.py tests/test_execution_streams.py tests/test_execution_runner.py tests/test_benchmark_execution.py -q
ruff check src/usagetrim/core/stream_redactor.py scripts/benchmark_execution.py tests/fixtures/execution/redactor_reference.py tests/test_stream_redactor.py tests/test_execution_streams.py tests/test_benchmark_execution.py
ruff format --check src/usagetrim/core/stream_redactor.py scripts/benchmark_execution.py tests/fixtures/execution/redactor_reference.py tests/test_stream_redactor.py tests/test_execution_streams.py tests/test_benchmark_execution.py
```

## Manual verification

The tests use real subprocesses in isolated, deadline-guarded test harnesses,
bounded summaries and synchronization events. Harness termination is test
cleanup, not a runner feature. No network or tokenizer assets are required.

```bash
PYTHONPATH=src pytest tests/test_execution_process.py tests/test_execution_runner.py tests/test_execution_shutdown.py tests/test_execution_contract.py tests/test_execution_artifacts.py tests/test_execution_streams.py tests/test_benchmark_execution.py -q
ruff check src/usagetrim/core/execution_runner.py src/usagetrim/core/execution_process.py src/usagetrim/core/darwin_wait.py src/usagetrim/core/execution_contract.py tests/test_execution_process.py tests/test_execution_runner.py tests/test_execution_shutdown.py tests/fixtures/execution/lifecycle.py
ruff format --check src/usagetrim/core/execution_runner.py src/usagetrim/core/execution_process.py src/usagetrim/core/darwin_wait.py src/usagetrim/core/execution_contract.py tests/test_execution_process.py tests/test_execution_runner.py tests/test_execution_shutdown.py tests/fixtures/execution/lifecycle.py
git diff --check
```

For the additional harness/test files:

```bash
ruff check scripts/benchmark_execution.py tests/test_benchmark_execution.py
ruff format --check scripts/benchmark_execution.py tests/test_benchmark_execution.py
```

Profiler regression checks (small inputs only):

```bash
PYTHONPATH=src pytest tests/test_execution_profile.py tests/test_benchmark_execution.py -q
ruff check scripts/execution_profile.py scripts/benchmark_execution.py tests/test_execution_profile.py tests/test_benchmark_execution.py
ruff format --check scripts/execution_profile.py scripts/benchmark_execution.py tests/test_execution_profile.py tests/test_benchmark_execution.py
```
