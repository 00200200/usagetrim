"""Explicit, bounded recovery for content search and complete redacted exports."""

import io
import os
import tempfile
import zlib
from pathlib import Path

from usagetrim.core.redactor import redact_secrets
from usagetrim.tui.cache_data import _metadata_connection

CONTENT_LIMIT = 4 * 1024 * 1024
STORED_LIMIT = CONTENT_LIMIT + 64 * 1024


class CacheContentError(ValueError):
    """Recovery cannot safely produce the complete content."""


def read_redacted_content(cache, ref_id):
    """Materialize only a bounded, complete payload, then redact before use."""
    with _metadata_connection(cache) as conn:
        row = (
            None
            if conn is None
            else conn.execute(
                "SELECT rowid,typeof(content) FROM output_cache WHERE ref_id=?", (ref_id,)
            ).fetchone()
        )
        if row is None:
            raise CacheContentError("Cache entry is missing or was removed.")
        if row[1] not in {"text", "blob"}:
            raise CacheContentError("Unsupported cache payload.")
        with conn.blobopen("output_cache", "content", row[0], readonly=True) as blob:
            if len(blob) > STORED_LIMIT:
                raise CacheContentError("Stored payload exceeds the safe size limit.")
            payload = blob.read(STORED_LIMIT)
    try:
        if row[1] == "text":
            data = payload
        elif payload.startswith(b"ZLIB") or payload.startswith(b"\x78"):
            decoder = zlib.decompressobj()
            data = decoder.decompress(
                payload[4:] if payload.startswith(b"ZLIB") else payload, CONTENT_LIMIT + 1
            )
            if len(data) > CONTENT_LIMIT:
                raise CacheContentError("Recovered content exceeds 4 MiB; operation refused.")
            if not decoder.eof or decoder.unused_data:
                raise CacheContentError("Compressed payload is incomplete or has trailing data.")
        elif payload.startswith((b"ZSTD", b"\x28\xb5\x2f\xfd")):
            try:
                import zstandard
            except ImportError:
                raise CacheContentError("Optional zstandard support is unavailable.") from None
            encoded = payload[4:] if payload.startswith(b"ZSTD") else payload
            try:
                size = zstandard.frame_content_size(encoded)
                # Unknown-size frames are refused rather than risking an
                # allocation or silently accepting a truncated streaming frame.
                if size < 0 or size > CONTENT_LIMIT:
                    raise CacheContentError("Zstd frame size is unknown or exceeds 4 MiB.")
                data = zstandard.ZstdDecompressor(max_window_size=4096).decompress(
                    encoded, max_output_size=CONTENT_LIMIT, allow_extra_data=False
                )
            except zstandard.ZstdError:
                raise CacheContentError("Damaged or oversized zstd payload.") from None
        else:
            data = payload
        if len(data) > CONTENT_LIMIT:
            raise CacheContentError("Recovered content exceeds 4 MiB; operation refused.")
        text = redact_secrets(data.decode("utf-8"))
        if len(text.encode("utf-8")) > CONTENT_LIMIT:
            raise CacheContentError("Redacted content exceeds 4 MiB; operation refused.")
        return text
    except (UnicodeError, zlib.error):
        raise CacheContentError("Damaged cache payload; operation refused.") from None


def search_content(cache, ref_id, query):
    """Return at most 20 literal, case-sensitive matching lines after redaction."""
    if not query or "\n" in query or "\r" in query:
        raise CacheContentError("Enter a nonempty single-line search term.")
    text = read_redacted_content(cache, ref_id)
    matches = []
    for number, line in enumerate(io.StringIO(text, newline=None), 1):
        line = line.rstrip("\r\n")
        if query in line:
            matches.append((number, line))
            if len(matches) == 20:
                break
    return matches


def export_content(cache, ref_id, destination):
    """Publish a complete 0600 file atomically, without replacing any target."""
    target = Path(destination)
    if os.path.lexists(target):
        raise FileExistsError("Export target already exists; refusing to overwrite.")
    text = read_redacted_content(cache, ref_id)
    fd, temporary = tempfile.mkstemp(prefix=".usagetrim-export-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike replace(), link() refuses existing files, including symlinks,
        # even if another process creates the target after the initial check.
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return target
