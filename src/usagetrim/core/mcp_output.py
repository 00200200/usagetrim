"""Lossless compaction of third-party MCP tool results.

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
from decimal import Decimal
from typing import Any

from usagetrim.metrics.tokenizer import count_tokens

_BOUNDARY = re.compile(
    r"^(<untrusted-data-([0-9a-f-]+)>\n)(.*?)(\n</untrusted-data-\2>)$", re.S | re.M
)
MIN_CHARS = 600
_MIN_ROWS = 3
# Unwrapping other single keys (e.g. "error") would hide what the value means.
_UNWRAP_KEYS = frozenset({"result"})


class _InexactNumber(ValueError):
    """A JSON number whose value would change on the way through a float."""


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


def compact_mcp_response(response: Any) -> Any:
    """Apply ``compact_mcp_text`` to a hook ``tool_response`` of any known shape."""
    if isinstance(response, str):
        return compact_mcp_text(response) or response
    if isinstance(response, list):
        return [compact_mcp_response(block) for block in response]
    if isinstance(response, dict):
        if response.get("type") == "text" and isinstance(response.get("text"), str):
            compact = compact_mcp_text(response["text"])
            return {**response, "text": compact} if compact else response
        if isinstance(response.get("content"), list):
            return {**response, "content": compact_mcp_response(response["content"])}
    return response
