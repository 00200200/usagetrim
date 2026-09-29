"""Protocol-safe compaction of third-party MCP tool results.

MCP ``CallToolResult`` values are not plain strings: they carry typed content
blocks, optional ``structuredContent``, ``isError``, annotations, and extension
fields such as ``_meta``. Flattening the whole payload into text risks either
missing compaction opportunities or changing what a client needs.

This module:

* applies existing lossless text/JSON rewrites only inside ``text`` blocks
  (and bare string responses);
* leaves ``structuredContent``, ``isError``, annotations, ``_meta``, unknown
  extension keys, and unsupported block shapes untouched;
* optionally replaces oversized opaque blocks (image/audio/resource) with a
  truthful size/type summary plus a local recovery ref (explicit opt-in).

MCP servers commonly return JSON rows that repeat every key per row, pretty-print
with indentation, or wrap a JSON document in a ``{"result": "..."}`` string so
every inner quote is escaped. These rewrites keep every value and type:

* a uniform array of objects becomes TSV with the keys once. A cell that parses
  as JSON *is* JSON (``51``, ``null``, ``"51"``); any other cell is a literal string.
* nested uniform arrays become ``{"columns": [...], "rows": [[...]]}``.
* JSON is re-emitted without indentation or ASCII escapes.

Prompt-injection boundaries such as Supabase's ``<untrusted-data-…>`` tags and
the surrounding warnings stay verbatim; only the JSON between them changes.
Output that would not get shorter is returned unchanged by the caller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from usagetrim.core.cache import ContextCache
from usagetrim.metrics.tokenizer import count_tokens

_BOUNDARY = re.compile(
    r"^(<untrusted-data-([0-9a-f-]+)>\n)(.*?)(\n</untrusted-data-\2>)$", re.S | re.M
)
MIN_CHARS = 600
_MIN_ROWS = 3
# Unwrapping other single keys (e.g. "error") would hide what the value means.
_UNWRAP_KEYS = frozenset({"result"})

# Content block types that cannot be rewritten losslessly as text.
OPAQUE_BLOCK_TYPES = frozenset({"image", "audio", "resource", "resource_link"})
# Default size floor before an opaque block may be replaced under reference policy.
DEFAULT_OPAQUE_BYTES = 8 * 1024

PolicyName = Literal["lossless", "reference"]


class _InexactNumber(ValueError):
    """A JSON number whose value would change on the way through a float."""


@dataclass(frozen=True)
class BlockSavings:
    """Token/byte delta for one content block (or the top-level string)."""

    index: int
    block_type: str
    action: Literal["unchanged", "compacted", "referenced", "passthrough"]
    raw_chars: int
    compact_chars: int
    raw_tokens: int
    compact_tokens: int
    ref_id: str | None = None

    @property
    def saved_chars(self) -> int:
        return max(0, self.raw_chars - self.compact_chars)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.raw_tokens - self.compact_tokens)


@dataclass(frozen=True)
class ResultSavings:
    """Aggregated savings for a CallToolResult or content-block list."""

    policy: PolicyName
    blocks: tuple[BlockSavings, ...]
    raw_chars: int
    compact_chars: int
    raw_tokens: int
    compact_tokens: int

    @property
    def saved_chars(self) -> int:
        return max(0, self.raw_chars - self.compact_chars)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.raw_tokens - self.compact_tokens)


@dataclass(frozen=True)
class CompactedMcpResult:
    """Compacted MCP payload plus a per-block savings report."""

    result: Any
    savings: ResultSavings


def _exact_float(literal: str) -> float:
    value = float(literal)
    # json.dumps writes repr(value); refuse numbers that would come back different,
    # such as NUMERIC columns with more significant digits than a double holds.
    if Decimal(literal) != Decimal(repr(value)):
        raise _InexactNumber(literal)
    return value


def _loads(text: str) -> Any:
    """Parse a payload that will be re-serialized, or raise if that would be lossy."""
    return json.loads(text, parse_float=_exact_float)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=1024)
def _parses_as_json(text: str) -> bool:
    # Deliberately lenient: any string that is valid JSON must be quoted in a cell,
    # including one holding a number too precise for a float.
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _plain(text: str) -> bool:
    """True when a string can appear unquoted in a TSV cell or header."""
    return bool(
        text
        and text == text.strip()
        and not any(ch in text for ch in "\t\n\r")
        # A bare closing tag at a line start could fake the end of untrusted data.
        and "untrusted-data" not in text
    )


def _cell(value: Any) -> str:
    if isinstance(value, str) and _plain(value) and not _parses_as_json(value):
        return value
    return _dump(value)


def _uniform_keys(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) < _MIN_ROWS:
        return None
    first = value[0]
    if not isinstance(first, dict) or not first:
        return None
    if any(not isinstance(row, dict) or row.keys() != first.keys() for row in value):
        return None
    return list(first)


def _columnize(value: Any) -> tuple[Any, bool]:
    keys = _uniform_keys(value)
    if keys is not None:
        rows = [[_columnize(row[key])[0] for key in keys] for row in value]
        return {"columns": keys, "rows": rows}, True
    if isinstance(value, dict):
        changed = False
        out = {}
        for key, item in value.items():
            out[key], item_changed = _columnize(item)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, list):
        pairs = [_columnize(item) for item in value]
        return [item for item, _ in pairs], any(changed for _, changed in pairs)
    return value, False


def _compact_value(value: Any) -> str:
    keys = _uniform_keys(value)
    if keys is not None and all(_plain(key) for key in keys):
        lines = ["\t".join(keys), *("\t".join(_cell(row[key]) for key in keys) for row in value)]
        header = f"[usagetrim: {len(value)} JSON rows as TSV; a cell that parses as JSON is JSON]"
        return header + "\n" + "\n".join(lines)
    columnized, changed = _columnize(value)
    if changed:
        return '[usagetrim: uniform object arrays as {"columns","rows"}]\n' + _dump(columnized)
    return _dump(value)


def _compact_boundaries(text: str) -> str | None:
    def replace(match: re.Match[str]) -> str:
        try:
            value = _loads(match[3])
        except ValueError:
            return match[0]
        return match[1] + _compact_value(value) + match[4]

    compact = _BOUNDARY.sub(replace, text)
    return compact if compact != text else None


def _transform(text: str) -> str | None:
    try:
        value = _loads(text)
    except ValueError:
        return _compact_boundaries(text)
    if isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _UNWRAP_KEYS:
        inner = next(iter(value.values()))
        if isinstance(inner, str):
            # The wrapper escapes every quote and newline of the real payload.
            return _transform(inner) or inner
    return _compact_value(value)


def compact_mcp_text(text: str) -> str | None:
    """Return a shorter lossless rendering of an MCP text result, or None."""
    if len(text) < MIN_CHARS:
        return None
    candidate = _transform(text)
    if candidate is None or candidate == text:
        return None
    if count_tokens(candidate).claude >= count_tokens(text).claude:
        return None
    return candidate


def _measure(text: str) -> tuple[int, int]:
    return len(text), count_tokens(text).claude if text else 0


def _block_type(block: Any) -> str:
    if not isinstance(block, dict):
        return "malformed"
    block_type = block.get("type")
    return block_type if isinstance(block_type, str) and block_type else "malformed"


def _opaque_byte_size(block: dict[str, Any]) -> int:
    return len(_dump(block).encode("utf-8"))


def _opaque_summary(block: dict[str, Any], *, ref_id: str, byte_size: int) -> str:
    block_type = _block_type(block)
    mime = block.get("mimeType") if isinstance(block.get("mimeType"), str) else None
    uri = None
    if block_type == "resource_link" and isinstance(block.get("uri"), str):
        uri = block["uri"]
    elif block_type == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict) and isinstance(resource.get("uri"), str):
            uri = resource["uri"]
    parts = [f"withheld {block_type} block"]
    if mime:
        parts.append(f"mimeType={mime}")
    if uri:
        parts.append(f"uri={uri}")
    parts.append(f"{byte_size:,} bytes JSON")
    detail = "; ".join(parts)
    return f"[UsageTrim MCP reference: {detail}. Full redacted block: usagetrim retrieve {ref_id}]"


def _reference_opaque(
    block: dict[str, Any],
    *,
    cache: ContextCache,
    threshold: int,
) -> tuple[dict[str, Any], str] | None:
    byte_size = _opaque_byte_size(block)
    if byte_size < threshold:
        return None
    payload = _dump(block)
    ref_id = cache.store(payload, source="mcp-result")
    notice = _opaque_summary(block, ref_id=ref_id, byte_size=byte_size)
    replacement: dict[str, Any] = {"type": "text", "text": notice}
    for key in ("annotations", "_meta"):
        if key in block:
            replacement[key] = block[key]
    return replacement, ref_id


def _compact_content_blocks(
    blocks: list[Any],
    *,
    policy: PolicyName,
    cache: ContextCache | None,
    opaque_bytes: int,
    allow_reference: bool,
) -> tuple[list[Any], list[BlockSavings]]:
    out: list[Any] = []
    savings: list[BlockSavings] = []
    for index, block in enumerate(blocks):
        block_type = _block_type(block)
        if not isinstance(block, dict) or block_type == "malformed":
            raw = _dump(block) if not isinstance(block, str) else block
            raw_chars, raw_tokens = _measure(raw if isinstance(raw, str) else str(raw))
            out.append(block)
            savings.append(
                BlockSavings(
                    index=index,
                    block_type=block_type,
                    action="passthrough",
                    raw_chars=raw_chars,
                    compact_chars=raw_chars,
                    raw_tokens=raw_tokens,
                    compact_tokens=raw_tokens,
                )
            )
            continue

        if block_type == "text" and isinstance(block.get("text"), str):
            original = block["text"]
            compact = compact_mcp_text(original)
            raw_chars, raw_tokens = _measure(original)
            if compact is None:
                out.append(block)
                savings.append(
                    BlockSavings(
                        index=index,
                        block_type="text",
                        action="unchanged",
                        raw_chars=raw_chars,
                        compact_chars=raw_chars,
                        raw_tokens=raw_tokens,
                        compact_tokens=raw_tokens,
                    )
                )
            else:
                compact_chars, compact_tokens = _measure(compact)
                # Preserve annotations, _meta, and any unknown extension keys.
                out.append({**block, "text": compact})
                savings.append(
                    BlockSavings(
                        index=index,
                        block_type="text",
                        action="compacted",
                        raw_chars=raw_chars,
                        compact_chars=compact_chars,
                        raw_tokens=raw_tokens,
                        compact_tokens=compact_tokens,
                    )
                )
            continue

        if (
            allow_reference
            and policy == "reference"
            and block_type in OPAQUE_BLOCK_TYPES
            and cache is not None
        ):
            referenced = _reference_opaque(block, cache=cache, threshold=opaque_bytes)
            raw = _dump(block)
            raw_chars, raw_tokens = _measure(raw)
            if referenced is not None:
                replacement, ref_id = referenced
                compact_chars, compact_tokens = _measure(replacement["text"])
                out.append(replacement)
                savings.append(
                    BlockSavings(
                        index=index,
                        block_type=block_type,
                        action="referenced",
                        raw_chars=raw_chars,
                        compact_chars=compact_chars,
                        raw_tokens=raw_tokens,
                        compact_tokens=compact_tokens,
                        ref_id=ref_id,
                    )
                )
                continue

        raw = _dump(block)
        raw_chars, raw_tokens = _measure(raw)
        out.append(block)
        savings.append(
            BlockSavings(
                index=index,
                block_type=block_type,
                action="passthrough",
                raw_chars=raw_chars,
                compact_chars=raw_chars,
                raw_tokens=raw_tokens,
                compact_tokens=raw_tokens,
            )
        )
    return out, savings


def _aggregate(policy: PolicyName, blocks: list[BlockSavings]) -> ResultSavings:
    return ResultSavings(
        policy=policy,
        blocks=tuple(blocks),
        raw_chars=sum(b.raw_chars for b in blocks),
        compact_chars=sum(b.compact_chars for b in blocks),
        raw_tokens=sum(b.raw_tokens for b in blocks),
        compact_tokens=sum(b.compact_tokens for b in blocks),
    )


def compact_mcp_result(
    response: Any,
    *,
    policy: PolicyName = "lossless",
    cache: ContextCache | None = None,
    opaque_bytes: int = DEFAULT_OPAQUE_BYTES,
) -> CompactedMcpResult:
    """Compact an MCP tool response while preserving protocol channels.

    ``policy="lossless"`` (default) only rewrites text with semantics-preserving
    rules. ``policy="reference"`` may replace oversized opaque blocks with a
    summary plus a recoverable cache ref; it never runs when ``isError`` is true,
    so error and safety-relevant payloads stay fully visible to the model.
    """
    if policy not in ("lossless", "reference"):
        raise ValueError(f"unsupported MCP compact policy: {policy!r}")

    if isinstance(response, str):
        compact = compact_mcp_text(response)
        raw_chars, raw_tokens = _measure(response)
        if compact is None:
            block = BlockSavings(
                index=0,
                block_type="text",
                action="unchanged",
                raw_chars=raw_chars,
                compact_chars=raw_chars,
                raw_tokens=raw_tokens,
                compact_tokens=raw_tokens,
            )
            return CompactedMcpResult(response, _aggregate(policy, [block]))
        compact_chars, compact_tokens = _measure(compact)
        block = BlockSavings(
            index=0,
            block_type="text",
            action="compacted",
            raw_chars=raw_chars,
            compact_chars=compact_chars,
            raw_tokens=raw_tokens,
            compact_tokens=compact_tokens,
        )
        return CompactedMcpResult(compact, _aggregate(policy, [block]))

    if isinstance(response, list):
        # Bare content-block list (common in Claude Code PostToolUse payloads).
        compacted, block_savings = _compact_content_blocks(
            response,
            policy=policy,
            cache=cache or (ContextCache() if policy == "reference" else None),
            opaque_bytes=opaque_bytes,
            allow_reference=True,
        )
        return CompactedMcpResult(compacted, _aggregate(policy, block_savings))

    if isinstance(response, dict):
        # Single content block passed alone.
        if response.get("type") == "text" and isinstance(response.get("text"), str):
            compacted, block_savings = _compact_content_blocks(
                [response],
                policy=policy,
                cache=None,
                opaque_bytes=opaque_bytes,
                allow_reference=False,
            )
            return CompactedMcpResult(compacted[0], _aggregate(policy, block_savings))

        if isinstance(response.get("content"), list):
            is_error = response.get("isError") is True
            # Errors stay fully visible: never spill opaque blocks to references.
            allow_reference = not is_error
            active_cache = None
            if allow_reference and policy == "reference":
                active_cache = cache or ContextCache()
            compacted, block_savings = _compact_content_blocks(
                response["content"],
                policy=policy,
                cache=active_cache,
                opaque_bytes=opaque_bytes,
                allow_reference=allow_reference,
            )
            # Copy the whole result so structuredContent, isError, _meta, and
            # unknown extension keys survive exactly; only content may change.
            result = {**response, "content": compacted}
            return CompactedMcpResult(result, _aggregate(policy, block_savings))

    # Unsupported shapes pass through unchanged with an empty savings report.
    empty = ResultSavings(
        policy=policy, blocks=(), raw_chars=0, compact_chars=0, raw_tokens=0, compact_tokens=0
    )
    return CompactedMcpResult(response, empty)


def compact_mcp_response(
    response: Any,
    *,
    policy: PolicyName = "lossless",
    cache: ContextCache | None = None,
    opaque_bytes: int = DEFAULT_OPAQUE_BYTES,
) -> Any:
    """Apply protocol-safe compaction; return only the updated payload.

    Prefer :func:`compact_mcp_result` when callers need per-block savings.
    """
    return compact_mcp_result(
        response, policy=policy, cache=cache, opaque_bytes=opaque_bytes
    ).result
