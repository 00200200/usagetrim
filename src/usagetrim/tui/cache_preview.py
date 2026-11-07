"""Explicit, bounded previews of legacy CCR payloads."""

import zlib

from usagetrim.tui.cache_data import _metadata_connection
from usagetrim.tui.formatting import display_text

LIMIT = 16 * 1024
STORED_LIMIT = 256 * 1024


def read_preview(cache, ref_id):
    """Reject large payloads before fetching; cap decompressed output before decoding."""
    with _metadata_connection(cache) as conn:
        if conn is None:
            return "Cache entry is unavailable."
        row = conn.execute(
            "SELECT typeof(content),rowid FROM output_cache WHERE ref_id=?",
            (ref_id,),
        ).fetchone()
        if row is None:
            return "Cache entry is unavailable."
        if row[0] not in {"text", "blob"}:
            return "Preview unavailable: unsupported stored payload."
        # Incremental BLOB access also supports SQLite TEXT and avoids fetching
        # an arbitrarily large value just to check its encoded byte length.
        with conn.blobopen("output_cache", "content", row[1], readonly=True) as blob:
            if len(blob) > STORED_LIMIT:
                return "Preview unavailable: stored payload exceeds the safe size limit."
            payload = blob.read(STORED_LIMIT)
    try:
        if row[0] == "text":
            data = payload
        elif payload.startswith(b"ZLIB") or payload.startswith(b"\x78"):
            decoder = zlib.decompressobj()
            data = decoder.decompress(
                payload[4:] if payload.startswith(b"ZLIB") else payload, LIMIT + 1
            )
            if not decoder.eof:
                return "Preview unavailable: compressed payload is oversized or incomplete."
            if decoder.unused_data:
                return "Preview unavailable: compressed payload has trailing data."
        elif payload.startswith((b"ZSTD", b"\x28\xb5\x2f\xfd")):
            try:
                import zstandard
            except ImportError:
                return "Preview unavailable: zstandard support is not installed."
            encoded = payload[4:] if payload.startswith(b"ZSTD") else payload
            try:
                size = zstandard.frame_content_size(encoded)
                # A known frame size overrides max_output_size in decompress().
                # Validate it first; unknown-size frames use the bounded buffer.
                if size != zstandard.CONTENTSIZE_UNKNOWN and size > LIMIT:
                    return "Preview unavailable: decoded payload exceeds 16 KiB."
                data = zstandard.ZstdDecompressor(max_window_size=1024).decompress(
                    encoded, max_output_size=LIMIT + 1, allow_extra_data=False
                )
            except zstandard.ZstdError:
                return "Preview unavailable: damaged or oversized zstd payload."
        else:
            data = payload
        if len(data) > LIMIT:
            return "Preview unavailable: decoded payload exceeds 16 KiB."
        text = display_text(data.decode("utf-8", errors="replace"))
        if len(text.encode("utf-8")) > LIMIT:
            return "Preview unavailable: redacted text exceeds 16 KiB."
        return "Preview limited to 100 lines and 16 KiB.\n" + "\n".join(text.splitlines()[:100])
    except (ValueError, zlib.error):
        return "Preview unavailable: damaged payload."
