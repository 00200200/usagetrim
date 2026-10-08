from __future__ import annotations

import json
import re
from typing import Any

from usagetrim.core.cache import ContextCache

_BASE64_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _truncate_base64_string(s: str) -> str | None:
    """Truncate base64 data URIs and raw base64 payloads to save tokens."""
    if s.startswith("data:") and ";base64," in s:
        prefix, b64_payload = s.split(";base64,", 1)
        raw_len = len(b64_payload)
        size_desc = f"{raw_len // 1024}KB" if raw_len >= 1024 else f"{raw_len} bytes"
        return f"{s[:32]}... [base64 payload {size_desc} omitted]"

    trimmed = s.strip()
    if (
        len(trimmed) >= 80
        and " " not in trimmed
        and _BASE64_PAYLOAD_RE.fullmatch(trimmed)
        and len(set(trimmed)) >= 10
        and (trimmed.endswith("=") or any(c in trimmed for c in "+/0123456789"))
    ):
        raw_len = len(trimmed)
        size_desc = f"{raw_len // 1024}KB" if raw_len >= 1024 else f"{raw_len} bytes"
        return f"{trimmed[:32]}... [base64 payload {size_desc} omitted]"

    return None


def _is_hateoas_link_container(key: str, val: Any) -> bool:
    """Check if key represents HATEOAS / HAL links."""
    return key.lower() in ("_links", "links", "hateoas") and isinstance(val, (dict, list))


def _fold_hateoas_links(
    links: dict[str, Any] | list[Any],
    max_array_items: int,
    max_string_len: int,
    max_depth: int,
    current_depth: int,
) -> Any:
    """Fold verbose HATEOAS link collections to keep essential links and save context."""
    if isinstance(links, dict):
        if len(links) <= 2:
            return {
                k: slim_json_data(
                    v,
                    max_array_items=max_array_items,
                    max_string_len=max_string_len,
                    max_depth=max_depth,
                    current_depth=current_depth + 1,
                )
                for k, v in links.items()
            }
        priority_order = ["self", "next", "prev", "first", "last"]
        chosen = [k for k in priority_order if k in links]
        for k in links:
            if k not in chosen and len(chosen) < 2:
                chosen.append(k)
        omitted = len(links) - len(chosen)
        res = {
            k: slim_json_data(
                links[k],
                max_array_items=max_array_items,
                max_string_len=max_string_len,
                max_depth=max_depth,
                current_depth=current_depth + 1,
            )
            for k in chosen
        }
        res["..."] = f"... [{omitted} more HATEOAS links omitted by usagetrim] ..."
        return res
    elif isinstance(links, list):
        if len(links) <= 2:
            return [
                slim_json_data(
                    v,
                    max_array_items=max_array_items,
                    max_string_len=max_string_len,
                    max_depth=max_depth,
                    current_depth=current_depth + 1,
                )
                for v in links
            ]
        kept = [
            slim_json_data(
                v,
                max_array_items=max_array_items,
                max_string_len=max_string_len,
                max_depth=max_depth,
                current_depth=current_depth + 1,
            )
            for v in links[:2]
        ]
        kept.append(f"... [{len(links) - 2} more HATEOAS links omitted by usagetrim] ...")
        return kept
    return links


def slim_json_data(
    data: Any,
    max_array_items: int = 3,
    max_string_len: int = 120,
    max_depth: int = 6,
    current_depth: int = 0,
) -> Any:
    """Recursively slim JSON structures by collapsing large arrays and long strings."""
    if current_depth >= max_depth:
        if isinstance(data, (dict, list)):
            return f"... [{len(data)} items omitted at max depth]"
        return data

    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if _is_hateoas_link_container(k, v):
                result[k] = _fold_hateoas_links(
                    v,
                    max_array_items=max_array_items,
                    max_string_len=max_string_len,
                    max_depth=max_depth,
                    current_depth=current_depth,
                )
            else:
                result[k] = slim_json_data(
                    v,
                    max_array_items=max_array_items,
                    max_string_len=max_string_len,
                    max_depth=max_depth,
                    current_depth=current_depth + 1,
                )
        return result

    if isinstance(data, list):
        if len(data) <= max_array_items:
            return [
                slim_json_data(
                    item,
                    max_array_items=max_array_items,
                    max_string_len=max_string_len,
                    max_depth=max_depth,
                    current_depth=current_depth + 1,
                )
                for item in data
            ]

        kept = [
            slim_json_data(
                item,
                max_array_items=max_array_items,
                max_string_len=max_string_len,
                max_depth=max_depth,
                current_depth=current_depth + 1,
            )
            for item in data[:max_array_items]
        ]
        omitted_count = len(data) - max_array_items
        sample_0 = data[0] if data else None
        sample_1 = data[1] if len(data) > 1 else None

        if isinstance(sample_0, dict) and isinstance(sample_1, dict):
            k0 = set(sample_0.keys())
            k1 = set(sample_1.keys())
            if k0 == k1 and k0:
                notice = (
                    f"... {omitted_count} more items with identical schema omitted by usagetrim ..."
                )
            elif k0 & k1:
                notice = (
                    f"... {omitted_count} more items with similar schema omitted by usagetrim ..."
                )
            else:
                keys = list(k0)[:4]
                hint = f" (sample keys: {keys})" if keys else ""
                notice = f"... {omitted_count} array items omitted by usagetrim{hint} ..."
        else:
            hint = ""
            if isinstance(sample_0, dict):
                keys = list(sample_0.keys())[:4]
                hint = f" (sample keys: {keys})"
            notice = f"... {omitted_count} array items omitted by usagetrim{hint} ..."

        kept.append(notice)
        return kept

    if isinstance(data, str):
        b64_truncated = _truncate_base64_string(data)
        if b64_truncated is not None:
            return b64_truncated
        if len(data) > max_string_len:
            omitted = len(data) - max_string_len
            return data[:max_string_len] + f"... [{omitted} chars omitted]"
        return data

    return data


def slim_json(
    text: str,
    max_array_items: int = 3,
    max_string_len: int = 120,
    max_depth: int = 6,
    cache_full: bool = True,
) -> str:
    """Parse and slim JSON text, caching full uncompressed data in SQLite CCR."""
    trimmed = text.strip()
    if not trimmed:
        return text

    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return text

    slimmed = slim_json_data(
        parsed,
        max_array_items=max_array_items,
        max_string_len=max_string_len,
        max_depth=max_depth,
    )
    result_str = json.dumps(slimmed, indent=2)

    # If significant reduction, cache raw in CCR and inject reference
    if cache_full and len(text) > len(result_str) * 1.3:
        cache = ContextCache()
        ref_id = cache.store(text, source="json_slimmer")
        header = f"// [usagetrim: raw JSON ({len(text):,} bytes) compacted. Ref: {ref_id}]\n"
        return header + result_str

    return result_str


def filter_har_log(raw_output: str, cache_full: bool = True) -> str:
    """Compact HTTP Archive (HAR) logs into a concise overview.

    Preserves request URL, method, status code, response mimeType,
    and slims large JSON/binary payloads while stripping verbose headers,
    cookies, and microsecond timings.
    """
    trimmed = raw_output.strip()
    if not trimmed:
        return raw_output

    try:
        data = json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return raw_output

    if not isinstance(data, dict) or "log" not in data or not isinstance(data["log"], dict):
        return raw_output

    har_log = data["log"]
    entries = har_log.get("entries")
    if not isinstance(entries, list):
        return raw_output

    compacted_entries: list[Any] = []
    limit = 2 if len(entries) > 3 else len(entries)
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request", {})
        resp = entry.get("response", {})
        content = resp.get("content", {})

        content_text = content.get("text")
        slimmed_content_text = content_text
        if isinstance(content_text, str):
            try:
                parsed_body = json.loads(content_text)
                slimmed_content_text = json.dumps(
                    slim_json_data(parsed_body, max_array_items=2, max_string_len=80),
                    separators=(",", ":"),
                )
            except Exception:
                b64 = _truncate_base64_string(content_text)
                if b64:
                    slimmed_content_text = b64
                elif len(content_text) > 120:
                    slimmed_content_text = (
                        content_text[:80] + f"... [{len(content_text) - 80} chars omitted]"
                    )

        compact_entry = {
            "startedDateTime": entry.get("startedDateTime"),
            "time_ms": entry.get("time"),
            "request": {
                "method": req.get("method"),
                "url": req.get("url"),
                "headers_count": len(req.get("headers", [])),
            },
            "response": {
                "status": resp.get("status"),
                "statusText": resp.get("statusText"),
                "mimeType": content.get("mimeType"),
                "size": content.get("size"),
                "content_preview": slimmed_content_text,
            },
        }
        compacted_entries.append(compact_entry)

    if len(entries) > limit:
        compacted_entries.append(
            f"... [{len(entries) - limit} more HAR entries omitted by usagetrim] ..."
        )

    compacted_har = {
        "log": {
            "version": har_log.get("version", "1.2"),
            "creator": har_log.get("creator"),
            "total_entries": len(entries),
            "entries": compacted_entries,
        }
    }

    result_str = json.dumps(compacted_har, indent=2)
    if cache_full and len(raw_output) > len(result_str) * 1.3:
        cache = ContextCache()
        ref_id = cache.store(raw_output, source="har_slimmer")
        header = f"// [usagetrim: raw HAR ({len(raw_output):,} bytes) compacted. Ref: {ref_id}]\n"
        return header + result_str

    return result_str


def filter_rest_response(
    raw_output: str,
    command: str = "",
    max_array_items: int = 2,
    cache_full: bool = True,
) -> str:
    """Compact REST API and curl response payloads.

    Handles pure JSON, HTTP headers + JSON body, and HAR logs.
    Achieves > 85% token reduction on large payloads.
    Caches full raw response in SQLite CCR cache with a recovery ref.
    """
    trimmed = raw_output.strip()
    if not trimmed:
        return raw_output

    if '"log"' in trimmed and '"entries"' in trimmed:
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, dict) and "log" in parsed:
                return filter_har_log(raw_output, cache_full=cache_full)
        except Exception:
            pass

    if trimmed.startswith("HTTP/") or "< HTTP/" in trimmed:
        lines = raw_output.splitlines()
        header_lines: list[str] = []
        body_lines: list[str] = []
        in_headers = True

        routine_headers = {
            "date",
            "server",
            "etag",
            "keep-alive",
            "connection",
            "vary",
            "x-powered-by",
            "x-process-time",
            "access-control-allow-origin",
            "access-control-allow-credentials",
            "strict-transport-security",
        }
        collapsed_hdr_count = 0

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if in_headers:
                if not stripped or (
                    not line.startswith("<")
                    and not stripped.startswith("HTTP/")
                    and ":" not in stripped
                    and not stripped.startswith("*")
                ):
                    in_headers = False
                    body_lines.extend(lines[idx:])
                    break

                cleaned_line = stripped
                if cleaned_line.startswith("< "):
                    cleaned_line = cleaned_line[2:].strip()

                if cleaned_line.startswith("HTTP/"):
                    header_lines.append(line)
                    continue

                if ":" in cleaned_line:
                    hdr_name = cleaned_line.split(":", 1)[0].strip().lower()
                    if hdr_name in routine_headers:
                        collapsed_hdr_count += 1
                        continue
                if stripped.startswith("* "):
                    continue

                header_lines.append(line)
            else:
                body_lines.append(line)

        if collapsed_hdr_count > 0:
            header_lines.append(
                f"< [... {collapsed_hdr_count} routine response headers collapsed ...]"
            )

        body_text = "\n".join(body_lines).strip()
        if body_text.startswith("{") or body_text.startswith("["):
            slimmed_body = slim_json(
                body_text,
                max_array_items=max_array_items,
                cache_full=False,
            )
            combined = "\n".join(header_lines) + "\n\n" + slimmed_body
            if cache_full and len(raw_output) > len(combined) * 1.3:
                cache = ContextCache()
                ref_id = cache.store(raw_output, source="rest_filter")
                header = (
                    f"// [usagetrim: raw REST response ({len(raw_output):,} bytes) "
                    f"compacted. Ref: {ref_id}]\n"
                )
                return header + combined
            return combined

    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            parsed = json.loads(trimmed)
            slimmed = slim_json_data(
                parsed,
                max_array_items=max_array_items,
                max_string_len=120,
                max_depth=6,
            )
            result_str = json.dumps(slimmed, indent=2)
            if cache_full and len(raw_output) > len(result_str) * 1.3:
                cache = ContextCache()
                ref_id = cache.store(raw_output, source="rest_filter")
                header = (
                    f"// [usagetrim: raw REST response ({len(raw_output):,} bytes) "
                    f"compacted. Ref: {ref_id}]\n"
                )
                return header + result_str
            return result_str
        except Exception:
            pass

    return raw_output
