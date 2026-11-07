# Incremental CCR execution artifacts (M2)

This internal API is not connected to `run`, MCP, a subprocess runner, or M1's
value types. It stores a sanitized decoded view, not original output bytes.
Existing content-addressed CCR references, compression formats, retrieval, FTS
search and the execution/result contract retain their existing behavior.

## API and lifecycle

```python
from usagetrim.core.cache import ContextCache

cache = ContextCache()
with cache.begin_artifact(quota_bytes=10 * 1024 * 1024) as writer:
    writer.append(b"first line\n")
    writer.append(b"second line\n")
    metadata = writer.finish()

view = cache.read_artifact(writer.ref_id, first_line=2, last_line=2, max_bytes=4096)
for fragment in cache.iter_artifact(writer.ref_id):
    consume(fragment.data)
```

`append` accepts bytes. It processes the supplied object in bounded windows;
callers remain responsible for how they obtain/allocate that object. `finish`
publishes `complete` only after flushing safe output and committing metadata.
The context manager aborts an unfinished write. `abort` retains safe buffered
records, discards the quarantined partial line, and records `incomplete`.
An append acknowledgement is not a durability acknowledgement for pending data.
`flush()` commits safe records already produced, without flushing a raw partial
line or finalizing UTF-8 decoding. Quota accounting inspects legacy entry lengths
via incremental BLOB handles once during migration; it never loads their bodies.
Subsequent reservations read a transactional accounting row rather than scanning
CCR. SQLite triggers track both artifact changes and legacy stores/eviction,
including changes from connections opened before accounting was initialized.

### Execution identity and independent streams

```python
with cache.begin_artifact(stream="stdout") as stdout:
    with cache.begin_artifact(stream="stderr", execution_id=stdout.execution_id) as stderr:
        first = stdout.append(b"partial line")
        second = stderr.append(b"diagnostic\n")
        third = stdout.append(b" completed\n")
        stdout.finish()
        stderr.finish()

recovery = cache.execution_recovery(stdout.execution_id)
observations = cache.iter_execution_observations(stdout.execution_id)
```

Each writer has an immutable `ArtifactStream` (`stdout`, `stderr`, or the existing
default `merged`) and its own decoder, redactor, pending line and output buffer.
Use one writer per source. A group cannot mix `merged` with separate streams or
register a source twice, even if one source was subsequently evicted. An execution
ID is generated automatically or must be a canonical lowercase UUID4 hex ID; use
a fresh ID for each execution, never a command, key or path. Validation is not
anonymization. Once an entire group has been evicted, reuse cannot be detected;
IDs must not be deliberately recycled.

`append` returns `ArtifactObservation`: execution ID, ref, source, input byte count
and a shared monotonically increasing sequence. The reservation is committed
before redaction and does not contain raw input, its hash, or exception text.
This records **recorder acceptance order**, not unknowable child emission order
across pipes. M3 must feed its collector's observations in the desired order;
competing writer threads are linearized at the database reservation. The record
does not assert that all supplied bytes were stored. A later processing/storage
failure can leave a recorded observation and an incomplete artifact.

Each sanitized chunk also identifies its execution/source and the conservative
`observation_first`/`observation_last` hull of its input dependencies. Those fields
are covered by the new domain-separated chunk checksum. A quarantined line or
UTF-8 code point can span several appends; a hull can include observations from
the other stream or earlier redaction context. These are **not exact byte-to-event
offsets**, output commit order, or instructions to concatenate sanitized chunks
into a faithful interleaved transcript. Payload order is exact within each stream;
observation order is independently retained as metadata. No raw staging is used
to reconstruct a merged view.
Chunk-origin metadata lives in a separate table, so the six-column payload table
and old checksums remain intact. Origin, payload and counters commit together.

`execution_recovery` reports the registered per-source refs and their individual
availability/completeness, plus allocated/retained observation counts. An evicted
source remains registered as missing while another source survives. An absent
slot means not registered, not a successful empty capture. No aggregate execution
success, child result or delivery status is inferred. The observation iterator
reads bounded pages, snapshots the current sequence ceiling, and closes each
connection before yielding. Eviction can leave gaps; the allocated and retained
counts are distinct. They are not proofs that every OS read was captured.

Pre-extension M2 artifacts retain `execution_id=None`, an unspecified/merged
view, no fabricated observation indices and their original chunk checksums.
They are readable even before the additive migration runs. M1 value types and
their existing merged-output semantics are unchanged.
Legacy CCR APIs remain usable across connections. Artifact writers should be
upgraded together: an older M2 writer does not know the new observation costs or
source contract, even though the payload table remains structurally compatible.

The same `cache.db` contains additive artifact, execution/observation and accounting
tables. Each artifact has an `output_cache` anchor and a random `tc_` reference
with the existing 16-hex shape. This new kind of ref is not content-addressed;
its full content digest covers the stored redacted UTF-8 view. Legacy refs retain
their content-addressed identity. No command, environment, or exception body is
stored in artifact metadata. Source is the fixed category `execution-artifact`.

Each commit adds at most 4 KiB of uncompressed UTF-8 plus bounded metadata,
sequence/line indices and a SHA-256 checksum covering the chunk and its indices.
Every committed prefix is valid UTF-8, including after interruption. Metadata
and chunk publication occur in one SQLite transaction. Connections have a finite
busy timeout, belong to the invoking thread, and close after each operation.
Artifact connections cap SQLite values/rows at 1 MiB to reject oversized corrupt
metadata. Legacy payload lengths are inspected through incremental handles;
legacy whole-text retrieval uses its existing connections and limits.
There is no background worker or queue. Concurrent appends to *one* writer are
rejected; separate writers are serialized only for SQLite transactions.

## Redaction and completeness

The new `execution-lines-v1` policy is intentionally more destructive than the
whole-text redactor. It quarantines each physical LF-delimited line before any
part is persisted. ANSI SGR color and OSC strings are parsed across fragment
boundaries, with no durable raw staging. CRLF becomes LF; bare CR, backspace and
DEL quarantine their whole line because they can overwrite visible credentials.
NUL/BEL are removed before detection. Unsupported CSI cursor/mode operations,
DCS, charset escapes and other control operations fail closed for the remainder:
this is not a terminal emulator. UTF-8 decoding errors use replacement. Whether a
replacement actually occurred remains unknown in the metadata.

Credential-looking lines are masked in their entirety: GitHub prefixes,
OpenAI/Anthropic prefixes, Bearer labels (case insensitive), AWS IDs/session
prefixes and credential labels, JWT prefixes, supported database URL schemes,
Gemini/Slack/Cloudflare patterns, and PEM private-key headers. A following
nonempty line is also quarantined for a credential value that could follow a
label on another line. All lines between PEM private-key markers are masked.
This deliberately over-redacts; malformed credential-looking text is not a
reason to publish it.

The line payload limit is 16 KiB of normalized UTF-8; a terminal sequence may
contain at most 4096 characters. These are buffer limits, **not assumptions about
maximum credential length**. Exceeding either limit, malformed/unfinished terminal
syntax, or an unterminated private key makes the capture incomplete. An overflow
permanently suppresses the unconfirmed line and remaining input; finish emits a
fixed unavailability notice. No raw fallback or raw spill is used.

Buffers have constant bounds independent of total log size: one quarantined line
(Python character/list overhead exceeds its 16 KiB payload), a decoder window,
one sanitized record and a 4 KiB pending output chunk. Incremental hashing retains
no whole-log copy. No vocabulary, network service or optional compression package
is needed.

This is pattern-based protection, not discovery of every possible secret. Unknown
credential formats, headerless private-key bodies, arbitrary encoding/obfuscation
or secrets in ordinary prose cannot be certified safe. `complete` means the full
input was consumed into this **redacted, normalized view**, not byte-for-byte raw
recovery, a successful child process, or perfect secret detection. M3 must not
advertise a complete raw log. No M1 returncode/termination value is inferred here.
Protection applies to newly appended data. Existing legacy records, free pages,
journals and backups are not scrubbed or securely erased; a database containing
old plaintext cannot be certified free of secrets by opting into this API.

## Recovery and integrity

`artifact_metadata` separates writing/complete/incomplete/missing/corrupt from
availability. Availability means a committed view exists (including a known
empty completed artifact), not that every byte has already been integrity-checked.
Completion is the writer's publication state; subsequent corruption can only be
discovered by reading the affected chunks. Read errors never expose SQLite/path
details or raw input.

`read_artifact` returns at most 64 KiB; callers may request a smaller positive
limit and an inclusive positive LF line range. Indexed line metadata locates the
first relevant chunk; only relevant chunks are read, without decompressing or
reassembling the full log. A response cap ends at a valid UTF-8 boundary.
`response_truncated` is independent of the underlying capture's completeness.
A line-range request intentionally selects a subset; it is not full recovery.

All selected chunk checksums are verified before a bounded read is returned.
A full bounded read also verifies the aggregate digest. Corruption returns no
text and state `corrupt`. Iteration snapshots the currently committed chunk count,
releases the database connection before each yield, and verifies the aggregate
digest at exhaustion. Consumers must not claim full recovery until iteration
successfully exhausts **and** its metadata says complete. A partial iteration
does not verify the entire artifact. Checksums detect corruption, not malicious
rewriting of both data and hashes by someone controlling the database.

Internal reads do not increment `hit_count`. Legacy `retrieve` recognizes this
new ref kind and returns a bounded view with explicit state/truncation metadata;
it never mistakes the empty anchor for full output. FTS search of execution
artifacts is not implemented; legacy `search` returns an explicit message for
this ref kind and continues unchanged for existing entries.

## Quotas, failures and abandoned writers

The aggregate logical quota uses the existing CCR cache budget (500 MiB by
default; existing environment overrides apply). `quota_bytes` may tighten it.
It includes legacy stored payload bytes, artifact bytes, 1024 bytes charged per
artifact, 256 per chunk, 512 per execution group and 256 per observation, including
zero-byte appends. Metadata therefore cannot grow outside quota. This does **not** bound SQLite's physical allocation,
free pages, rollback-journal peaks, or old FTS tables. A hard filesystem quota is
outside this implementation. New writes evict eligible CCR entries using the
existing LRI order and bounded candidate queries; active writers are protected.
Eviction/clear removes chunks with their CCR anchor, including through legacy
connections without foreign keys enabled. An explicit `clear()` may invalidate
active writers; they cannot silently recreate their entries.

Quota checks and all accounting updates share the payload/metadata transaction;
rollback also rolls back accounting and eviction. The first migration seeds byte
lengths without copying old payloads. Triggers avoid reading `OLD.content` during
eviction, using the saved sizes instead. The write hot path reads one accounting
row; no process-local cached total can become stale across writers. Payload
commits remain bounded at 4 KiB; append's observation reservation also refreshes
the lease, and an explicit data flush does not need a second heartbeat commit.
Readers keep short, independent chunk transactions rather than pooled
cross-thread connections or read locks held across consumer yields. The final
empty read transaction is skipped once the snapshot's chunk count is exhausted.
These changes reduce deterministic work; no throughput improvement is claimed
without runtime measurement.

At most four writing artifacts may exist in the database, including across
processes. `max_active` can lower this limit. Writes use a five-minute lease.
Append/flush and explicit `heartbeat()` refresh it. A new writer or
`expire_artifact_writes()` fences expired leases as incomplete; it cannot prove
whether an idle process died. An idle live writer must heartbeat. An expired
writer cannot resume, even if it later wakes. No timer/thread is installed.

Quota and SQLite/disk/permission failures close the writer and attempt to mark it
incomplete while retaining earlier committed safe chunks. A completely unwritable
database may prevent that update: the existing state remains `writing`, never
`complete`, and can be reclaimed by lease expiry when storage becomes writable.
A process crash loses in-memory buffers but cannot publish completion. SQLite
transaction durability is governed by the database/filesystem; no promise is made
for power failure. No forceful thread cancellation is attempted.

## Remaining milestones / validation

M3–M5 must integrate capture and recovery with the runner, CLI and MCP, supply
heartbeats/lifecycle ownership, and map storage results to M1 without conflating
child exit with storage errors. Additional provider/output-specific redaction
policies and physical-disk quota management may be needed before claiming wider
capture coverage. These APIs make no bounded-memory claim about today's runners
or legacy whole-text cache methods.

Suggested local checks (not run during implementation):

```sh
PYTHONPATH=src pytest tests/test_execution_streams.py tests/test_stream_redactor.py tests/test_execution_artifacts.py tests/test_cache.py tests/test_cache_policy.py tests/test_redactor.py tests/test_execution_contract.py tests/test_execution_compatibility.py -q
ruff check src/usagetrim/core/artifact_quota.py src/usagetrim/core/cache.py src/usagetrim/core/execution_artifacts.py src/usagetrim/core/stream_redactor.py tests/test_execution_streams.py tests/test_stream_redactor.py tests/test_execution_artifacts.py
ruff format --check src/usagetrim/core/artifact_quota.py src/usagetrim/core/cache.py src/usagetrim/core/execution_artifacts.py src/usagetrim/core/stream_redactor.py tests/test_execution_streams.py tests/test_stream_redactor.py tests/test_execution_artifacts.py
git diff --check
```
