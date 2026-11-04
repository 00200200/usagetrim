"""Columnar and TOON (Token-Optimized Object Notation) table compressor for API and DB payloads."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from usagetrim.core.cache import ContextCache
from usagetrim.core.redactor import redact_secrets
from usagetrim.metrics.tokenizer import count_model_tokens, count_tokens, model_counting


@dataclass
class TableResult:
    original_tokens: int
    compacted_tokens: int
    saved_tokens: int
    reduction_pct: float
    ref_id: str | None
    row_count: int
    column_count: int
    columns: list[str]
    text: str
    format_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "compacted_tokens": self.compacted_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
            "ref_id": self.ref_id,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "columns": self.columns,
            "text": self.text,
            "format_type": self.format_type,
        }


def _parse_tabular_input(raw: str) -> tuple[list[str], list[list[str]]]:
    """Parse JSON or CSV/TSV into headers and row values."""
    trimmed = raw.strip()

    # 1. Try parsing JSON
    if (trimmed.startswith("[") and trimmed.endswith("]")) or (
        trimmed.startswith("{") and trimmed.endswith("}")
    ):
        try:
            parsed = json.loads(trimmed)
            records: list[dict[str, Any]] = []
            if isinstance(parsed, list):
                if all(isinstance(x, dict) for x in parsed):
                    records = parsed
            elif isinstance(parsed, dict):
                # Search for primary list attribute (e.g. "items", "data", "results", "users")
                for key in ("items", "data", "results", "rows", "records", "users", "entries"):
                    val = parsed.get(key)
                    if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                        records = val
                        break
                if not records:
                    for val in parsed.values():
                        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                            records = val
                            break

            if records:
                # Collect unified ordered keys
                keys: list[str] = []
                for rec in records:
                    for k in rec.keys():
                        if k not in keys:
                            keys.append(str(k))
                rows: list[list[str]] = []
                for rec in records:
                    rows.append([str(rec.get(k, "")) for k in keys])
                return keys, rows
        except Exception:
            pass

    # 2. Try parsing SQL / ASCII grid table
    sql_headers, sql_rows = _parse_sql_ascii_table(trimmed)
    if sql_headers and sql_rows:
        return sql_headers, sql_rows

    # 3. Try parsing CSV / TSV / PSV
    delimiter = ","
    if "\t" in trimmed and "," not in trimmed:
        delimiter = "\t"
    elif "|" in trimmed and "," not in trimmed and not trimmed.startswith("+"):
        delimiter = "|"

    try:
        reader = csv.reader(io.StringIO(trimmed), delimiter=delimiter)
        csv_rows = list(reader)
        if len(csv_rows) >= 2 and all(len(r) == len(csv_rows[0]) for r in csv_rows[:10]):
            headers = [h.strip() for h in csv_rows[0]]
            data_rows = [[c.strip() for c in r] for r in csv_rows[1:]]
            return headers, data_rows
    except Exception:
        pass

    return [], []


def _parse_sql_ascii_table(trimmed: str) -> tuple[list[str], list[list[str]]]:
    """Parse ASCII/Unicode grid tables from MySQL, PostgreSQL, SQLite, DuckDB, etc."""
    lines = [line.rstrip() for line in trimmed.splitlines() if line.strip()]
    if len(lines) < 2:
        return [], []

    # Format 1: Bordered box table (+----+----+ or | id | name |)
    if any(line.startswith("+") and line.endswith("+") and "-" in line for line in lines[:3]):
        table_lines = [
            line
            for line in lines
            if not (line.startswith("+") and line.endswith("+") and "-" in line)
        ]
        if table_lines:
            header_line = table_lines[0]
            if header_line.startswith("|") and header_line.endswith("|"):
                headers = [c.strip() for c in header_line.split("|")[1:-1]]
                data_rows: list[list[str]] = []
                for row_line in table_lines[1:]:
                    if row_line.startswith("|") and row_line.endswith("|"):
                        cells = [c.strip() for c in row_line.split("|")[1:-1]]
                        if len(cells) == len(headers):
                            data_rows.append(cells)
                if headers and data_rows:
                    return headers, data_rows

    # Format 2: PostgreSQL / psql style (id | name \n----+----)
    for i in range(min(4, len(lines) - 1)):
        sep = lines[i + 1].strip()
        if re.match(r"^[-+\s]+$", sep) and "+" in sep and "-" in sep:
            header_line = lines[i]
            if "|" in header_line:
                headers = [c.strip() for c in header_line.split("|")]
                data_rows: list[list[str]] = []
                for row_line in lines[i + 2 :]:
                    if re.match(r"^\(\d+\s+rows?\)", row_line.strip(), re.IGNORECASE):
                        break
                    if "|" in row_line:
                        cells = [c.strip() for c in row_line.split("|")]
                        if len(cells) == len(headers):
                            data_rows.append(cells)
                if headers and data_rows:
                    return headers, data_rows

    # Format 3: SQLite / CLI columnar dashes (----------  ----------)
    for i in range(min(4, len(lines) - 1)):
        sep = lines[i + 1].strip()
        if re.match(r"^-+(?:\s+-+)+$", sep):
            spans = [m.span() for m in re.finditer(r"-+", sep)]
            if len(spans) >= 2:
                header_line = lines[i]
                headers = [header_line[s:e].strip() for s, e in spans]
                data_rows: list[list[str]] = []
                for row_line in lines[i + 2 :]:
                    if not row_line.strip() or row_line.startswith("("):
                        continue
                    cells = [row_line[s:e].strip() if s < len(row_line) else "" for s, e in spans]
                    data_rows.append(cells)
                if headers and data_rows:
                    return headers, data_rows

    return [], []


@model_counting
def compact_table(
    raw_input: str,
    budget: int = 2000,
    format_type: str = "toon",
    *,
    model: str | None = None,
) -> TableResult:
    """Convert JSON object arrays or CSV data into token-efficient TOON or markdown table."""
    if model is not None:
        count_model_tokens("", model)

    def size(value: str) -> int:
        if model is not None:
            return count_model_tokens(value, model).count
        return count_tokens(value).openai

    redacted = redact_secrets(raw_input)
    original_tokens = size(redacted)

    headers, rows = _parse_tabular_input(redacted)
    if not headers or not rows:
        # Fallback to returning original text if tabular parsing fails
        fallback, fallback_tokens, fallback_ref = redacted, original_tokens, None
        if model is not None and original_tokens > budget:
            from usagetrim.core.adaptive import _compress_to_budget_with_ref

            fallback, fallback_ref = _compress_to_budget_with_ref(
                redacted, budget, source="table-compaction", model=model
            )
            fallback_tokens = size(fallback)
        saved = max(0, original_tokens - fallback_tokens)
        return TableResult(
            original_tokens=original_tokens,
            compacted_tokens=fallback_tokens,
            saved_tokens=saved,
            reduction_pct=round(saved / original_tokens * 100, 1) if original_tokens else 0.0,
            ref_id=fallback_ref,
            row_count=0,
            column_count=0,
            columns=[],
            text=fallback,
            format_type="raw",
        )

    cache = ContextCache()
    ref_id = cache.store(redacted, source="table-compaction")

    # Format into TOON or Markdown
    def _render(kept_rows: list[list[str]], omitted: int = 0) -> str:
        lines: list[str] = []
        if format_type.lower() == "markdown":
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
            for r in kept_rows:
                lines.append("| " + " | ".join(r) + " |")
        else:
            # TOON (Token-Optimized Object Notation)
            lines.append("[" + " | ".join(headers) + "]")
            for r in kept_rows:
                lines.append(" | ".join(r))

        if omitted > 0:
            lines.append(
                f"\n[... {omitted} rows omitted to fit token budget. Ref: {ref_id} - usagetrim retrieve {ref_id} ...]"
            )
        return redact_secrets("\n".join(lines))

    full_rendered = _render(rows)
    current_tokens = size(full_rendered)

    # Budget enforcement loop
    kept_rows = list(rows)
    omitted = 0
    if current_tokens > budget and len(rows) > 4:
        target_rows = max(4, int(len(rows) * (budget / current_tokens) * 0.8))
        head_count = target_rows // 2
        tail_count = target_rows - head_count
        head = rows[:head_count]
        tail = rows[-tail_count:] if tail_count > 0 else []
        omitted = len(rows) - len(head) - len(tail)
        kept_rows = head + tail
        full_rendered = _render(kept_rows, omitted=omitted)
        current_tokens = size(full_rendered)

        while current_tokens > budget and len(kept_rows) > 4:
            if len(head) >= len(tail) and head:
                head.pop()
            elif tail:
                tail.pop(0)
            omitted = len(rows) - len(head) - len(tail)
            kept_rows = head + tail
            full_rendered = _render(kept_rows, omitted=omitted)
            current_tokens = size(full_rendered)

    if model is not None and current_tokens > budget:
        from usagetrim.core.adaptive import compress_to_budget

        full_rendered = compress_to_budget(
            full_rendered, budget, original_text=redacted, source="table-compaction", model=model
        )
        current_tokens = size(full_rendered)
    saved = max(0, original_tokens - current_tokens)
    pct = round((saved / original_tokens) * 100, 1) if original_tokens else 0.0

    return TableResult(
        original_tokens=original_tokens,
        compacted_tokens=current_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        ref_id=ref_id,
        row_count=len(rows),
        column_count=len(headers),
        columns=headers,
        text=full_rendered,
        format_type=format_type,
    )
