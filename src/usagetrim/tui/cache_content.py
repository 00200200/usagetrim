"""Explicit, bounded recovery for content search and complete redacted exports."""

import hashlib
import io
import os
import re
import tempfile
import zlib
from pathlib import Path

from usagetrim.core.redactor import redact_secrets
from usagetrim.tui.cache_data import _metadata_connection

CONTENT_LIMIT = 4 * 1024 * 1024
STORED_LIMIT = CONTENT_LIMIT + 64 * 1024


class CacheContentError(ValueError):
    """Recovery cannot safely produce the complete content."""

    def __init__(self, message, *, reason="invalid"):
        super().__init__(message)
        self.reason = reason


def read_redacted_content(
    cache, ref_id, *, max_bytes=None, max_stored_bytes=None, verify_hash=False
):
    """Materialize a bounded complete payload, optionally verify it, then redact.

    Hash verification uses the original decompressed UTF-8 bytes, before renewed
    redaction. content_hash is authoritative: ref_id is only a shortened lookup
    key and may also incorporate a namespace in ContextCache.store().
    """
    max_bytes = CONTENT_LIMIT if max_bytes is None else max_bytes
    max_stored_bytes = STORED_LIMIT if max_stored_bytes is None else max_stored_bytes
    if type(max_bytes) is not int or not 0 < max_bytes <= CONTENT_LIMIT:
        raise ValueError("Invalid content limit")
    if type(max_stored_bytes) is not int or not 0 < max_stored_bytes <= STORED_LIMIT:
        raise ValueError("Invalid stored-payload limit")
    limit_label = "4 MiB" if max_bytes == CONTENT_LIMIT else f"{max_bytes:,} bytes"
    with _metadata_connection(cache) as conn:
        if verify_hash and conn is not None:
            columns = {column[1] for column in conn.execute("PRAGMA table_info(output_cache)")}
            if "content_hash" not in columns:
                raise CacheContentError(
                    "Snapshot integrity metadata is unavailable.", reason="integrity"
                )
        # Fetch at most 65 characters, so an oversized metadata value cannot be
        # materialized in Python or mistaken for a valid 64-character digest.
        hash_column = (
            ",CASE WHEN typeof(content_hash)='text' THEN substr(content_hash,1,65) ELSE NULL END"
            if verify_hash
            else ""
        )
        row = (
            None
            if conn is None
            else conn.execute(
                f"SELECT rowid,typeof(content){hash_column} FROM output_cache WHERE ref_id=?",
                (ref_id,),
            ).fetchone()
        )
        if row is None:
            raise CacheContentError("Cache entry is missing or was removed.", reason="missing")
        if row[1] not in {"text", "blob"}:
            raise CacheContentError("Unsupported cache payload.")
        if verify_hash and (
            not isinstance(row[2], str) or re.fullmatch(r"[0-9a-f]{64}", row[2]) is None
        ):
            raise CacheContentError(
                "Snapshot integrity metadata is unavailable.", reason="integrity"
            )
        with conn.blobopen("output_cache", "content", row[0], readonly=True) as blob:
            if len(blob) > max_stored_bytes:
                raise CacheContentError(
                    "Stored payload exceeds the safe size limit.", reason="limit"
                )
            payload = blob.read(max_stored_bytes)
    try:
        if row[1] == "text":
            data = payload
        elif payload.startswith(b"ZLIB") or payload.startswith(b"\x78"):
            decoder = zlib.decompressobj()
            data = decoder.decompress(
                payload[4:] if payload.startswith(b"ZLIB") else payload, max_bytes + 1
            )
            if len(data) > max_bytes:
                raise CacheContentError(
                    f"Recovered content exceeds {limit_label}; operation refused.", reason="limit"
                )
            if not decoder.eof or decoder.unused_data:
                raise CacheContentError("Compressed payload is incomplete or has trailing data.")
        elif payload.startswith((b"ZSTD", b"\x28\xb5\x2f\xfd")):
            try:
                import zstandard
            except ImportError:
                raise CacheContentError(
                    "Optional zstandard support is unavailable.", reason="unavailable"
                ) from None
            encoded = payload[4:] if payload.startswith(b"ZSTD") else payload
            try:
                size = zstandard.frame_content_size(encoded)
                # Unknown-size frames are refused rather than risking an
                # allocation or silently accepting a truncated streaming frame.
                if size < 0 or size > max_bytes:
                    raise CacheContentError(
                        f"Zstd frame size is unknown or exceeds {limit_label}.",
                        reason="unavailable" if size < 0 else "limit",
                    )
                data = zstandard.ZstdDecompressor(
                    max_window_size=max(1024, (max_bytes + 1023) // 1024)
                ).decompress(encoded, max_output_size=max_bytes, allow_extra_data=False)
            except zstandard.ZstdError:
                raise CacheContentError("Damaged or oversized zstd payload.") from None
        else:
            data = payload
        if len(data) > max_bytes:
            raise CacheContentError(
                f"Recovered content exceeds {limit_label}; operation refused.", reason="limit"
            )
        decoded = data.decode("utf-8")
        if verify_hash and hashlib.sha256(data).hexdigest() != row[2]:
            raise CacheContentError("Snapshot integrity verification failed.", reason="integrity")
        text = redact_secrets(decoded)
        if len(text.encode("utf-8")) > max_bytes:
            raise CacheContentError(
                f"Redacted content exceeds {limit_label}; operation refused.", reason="limit"
            )
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
