from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from usagetrim import __version__
from usagetrim.core.adaptive import compress_to_budget
from usagetrim.core.cache import ContextCache
from usagetrim.core.cleaner import CleanerOptions, compact_terminal_output
from usagetrim.core.companion_state import already_wrapped, client_name, paused
from usagetrim.core.diff_slimmer import budgeted_diff_slice
from usagetrim.core.gain import build_gain_report
from usagetrim.core.json_slimmer import slim_json
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.safe_filter import safe_compact_output
from usagetrim.core.skeleton import extract_symbol_or_range
from usagetrim.core.specialized import auto_specialize_command_output
from usagetrim.core.telemetry import record_text, recovery_engine
from usagetrim.core.tree_scanner import format_tree_as_text, scan_directory
from usagetrim.metrics.tokenizer import count_tokens

# Global session metrics accumulator
_SESSION_SAVED_CLAUDE = 0
_SESSION_SAVED_OPENAI = 0
_SESSION_SAVED_GEMINI = 0


TOOLS_DEFINITIONS = [
    {
        "name": "usagetrim_context",
        "description": "Save/read/list/forget bounded task checkpoints. Use the root/task from the session hook; save at milestones, not every turn. expected_revision=0 creates, otherwise use the last read revision. Never stores transcripts or calls AI. Notes are fallible data; current user instructions win.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["save", "read", "list", "forget"]},
                "root": {
                    "type": "string",
                    "description": "Absolute directory; tasks are isolated by directory and ID.",
                },
                "task": {
                    "type": "string",
                    "description": "Session-specific task ID from the hook, or an explicit separate task ID.",
                },
                "retention": {
                    "type": "string",
                    "enum": ["permanent", "ephemeral"],
                    "description": "For save: new tasks default to ephemeral; permanent excludes the task from TTL cleanup. Omit to preserve an existing task's class.",
                },
                "expected_revision": {"type": "integer"},
                "revision": {
                    "type": "integer",
                    "description": "Read a retained earlier revision; default latest.",
                },
                "checkpoint": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        **{
                            field: {"type": "array", "items": {"type": "string"}}
                            for field in (
                                "constraints",
                                "decisions",
                                "progress",
                                "next_steps",
                                "references",
                            )
                        },
                    },
                    "required": ["goal"],
                    "additionalProperties": False,
                },
            },
            "required": ["action", "root"],
        },
    },
    {
        "name": "usagetrim_code",
        "description": "Search a local syntax index or get a ranked repo map. Use symbols for definitions, occurrences for raw matches, outline for module hierarchy, references or callers to find symbol callers across project (0 LSP daemons), search for text, pattern for ast-grep patterns. Then read with usagetrim_read or edit with usagetrim_edit_symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Absolute project directory; respects Git ignores.",
                },
                "mode": {
                    "type": "string",
                    "enum": [
                        "map",
                        "symbols",
                        "occurrences",
                        "search",
                        "pattern",
                        "outline",
                        "references",
                        "callers",
                    ],
                },
                "query": {"type": "string"},
                "file": {"type": "string", "description": "Optional file relative to root."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
            },
            "required": ["root"],
        },
    },
    {
        "name": "usagetrim_edit_symbol",
        "description": "Precisely replace a class, function, or method declaration in an absolute source file by symbol selector, with diff preview and hash-guarded atomic updates. Replaces heavyweight LSP tools.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute source file path.",
                },
                "selector": {
                    "type": "string",
                    "description": "Qualified symbol name, e.g. 'Class.method' or 'function_name', optionally '@line'.",
                },
                "replacement": {
                    "type": "string",
                    "description": "New source text for the symbol declaration and body.",
                },
                "expected_hash": {
                    "type": "string",
                    "description": "SHA-256 digest of the entire file prior to modification (returned by usagetrim_read).",
                },
                "apply": {
                    "type": "boolean",
                    "description": "Set true to write the modification to disk atomically; false (default) returns a unified diff preview.",
                    "default": False,
                },
            },
            "required": ["path", "selector", "replacement", "expected_hash"],
        },
    },
    {
        "name": "usagetrim_exec",
        "description": "Run a noninteractive shell command with conservative log filtering and exit status. Set max_tokens only to opt into truncation; shortened output has a recovery ref.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Opt into truncation with this line limit. Omit to preserve diagnostics and unknown output.",
                },
                "budget": {
                    "type": "integer",
                    "description": "Compatibility alias for max_tokens; explicitly permits truncation.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "usagetrim_read",
        "description": "Read a file, outline, symbol, or line range with bounded output. Prefer targeted reads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "skeleton": {
                    "type": "boolean",
                    "description": "If true, extracts structural outline/AST (functions, classes, types) eliding method bodies.",
                    "default": False,
                },
                "lines": {
                    "type": "string",
                    "description": "Specific line range to read (e.g. '10-45').",
                },
                "symbol": {
                    "type": "string",
                    "description": "Exact qualified name, e.g. Cache.get, optionally @line for ambiguity. Returns original source and file hash.",
                },
                "strip_comments": {
                    "type": "boolean",
                    "description": "If true, strip comments and blank lines to reduce context consumption.",
                    "default": False,
                },
                "if_modified_since_hash": {
                    "type": "string",
                    "description": "SHA-256 hash or prefix from previous read. If unchanged, returns short 304 Not Modified notice.",
                },
                "include_hash": {
                    "type": "boolean",
                    "description": "If true, prepends the file's SHA-256 hash header for conditional re-reads.",
                    "default": False,
                },
                "auto_skeleton": {
                    "type": "boolean",
                    "description": "If true and file exceeds budget without symbol/lines, returns structural AST outline.",
                    "default": False,
                },
                "types_only": {
                    "type": "boolean",
                    "description": "If true, extracts pure type definition / interface stubs (.d.ts / .pyi) eliding function bodies.",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "usagetrim_retrieve",
        "description": "Recover redacted cached output by ref. Specify lines or query to return only relevant chunks; every recovery is charged as added context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search words in this cached output; cannot combine with lines.",
                },
                "ref_id": {
                    "type": "string",
                    "description": "The ref ID returned by usagetrim in a truncated log notice.",
                },
                "lines": {
                    "type": "string",
                    "description": "Optional specific line range to retrieve (e.g. '40-100').",
                },
            },
            "required": ["ref_id"],
        },
    },
    {
        "name": "usagetrim_diff",
        "description": "Summarize git diff, folding lockfiles and generated files. Recover omitted changes by ref before reviewing them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "If true, view staged changes (--cached).",
                    "default": False,
                },
                "path": {
                    "type": "string",
                    "description": "Optional file or directory path to scope git diff.",
                },
                "budget": {
                    "type": "integer",
                    "description": "Optional token budget ceiling for the diff (e.g. 2000). High-priority code files retain full hunks; low-priority and overflow files are folded.",
                },
                "ignore_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional regex patterns of files to fold in the diff (e.g. test snapshots or fixtures).",
                },
            },
        },
    },
    {
        "name": "usagetrim_tree",
        "description": "Analyze directory token distribution and locate oversized files/directories (e.g. lockfiles, test fixtures) that consume disproportionate context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Root directory to scan (default '.').",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum folder depth (default 3).",
                    "default": 3,
                },
            },
        },
    },
    {
        "name": "usagetrim_json",
        "description": "Preview JSON arrays, strings and nested data with bounded output. Omitted values and schema variants must be recovered from the redacted cache before relying on them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "json_str": {
                    "type": "string",
                    "description": "Raw JSON string to compact.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional file path containing JSON to compact.",
                },
                "max_items": {
                    "type": "integer",
                    "description": "Maximum array items to keep per list (default 3).",
                    "default": 3,
                },
            },
        },
    },
    {
        "name": "usagetrim_clip",
        "description": "Compact noisy text, logs, diffs, JSON, or stack traces with secret redaction and CCR recovery caching before pasting or sending in context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The raw noisy text to compact.",
                },
                "budget": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 32000,
                    "default": 2000,
                    "description": "Target token budget ceiling for compacted output.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "usagetrim_pack",
        "description": "Pack multiple source files or directories into an AI-optimized prompt bundle with AST skeletonization, secret scrubbing, and token budget management.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Absolute project root directory.",
                },
                "paths": {
                    "type": "array",
                    "description": "List of relative or absolute file/directory paths to bundle.",
                },
                "budget": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 64000,
                    "default": 4000,
                    "description": "Token budget ceiling across all packed files.",
                },
                "skeleton": {
                    "type": "boolean",
                    "description": "If true, force AST structural skeletons for all code files.",
                    "default": False,
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "usagetrim_distill",
        "description": "Distill multi-turn chat transcripts or session histories into dense executive context (goals, decisions, modified files, active state) with zero loss CCR recovery.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {
                    "type": "string",
                    "description": "Raw multi-turn conversation text or transcript.",
                },
                "budget": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 32000,
                    "default": 1500,
                    "description": "Target token budget ceiling for distilled context.",
                },
            },
            "required": ["transcript"],
        },
    },
    {
        "name": "usagetrim_table",
        "description": "Compress JSON arrays of objects, CSV, or TSV data into compact TOON (Token-Optimized Object Notation) or Markdown table, eliminating repeated key bloat by 60-75%.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "Raw JSON object array, CSV, or TSV string.",
                },
                "budget": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 32000,
                    "default": 2000,
                    "description": "Target token budget ceiling.",
                },
                "format": {
                    "type": "string",
                    "enum": ["toon", "markdown"],
                    "default": "toon",
                    "description": "Output format: 'toon' (dense delimited) or 'markdown' table.",
                },
            },
            "required": ["data"],
        },
    },
    {
        "name": "usagetrim_optimize",
        "description": "Unified autonomous context optimizer. Self-routes prompts, intra-fence code blocks (JSON -> TOON, diffs -> slim), conversation transcripts, and noisy logs with secret redaction and 0-loss CCR guarantee.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The raw prompt, code, log, table, or conversation content to optimize.",
                },
                "budget": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 64000,
                    "default": 2000,
                    "description": "Target token budget ceiling for optimized context.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "usagetrim_stats",
        "description": "Report estimated net output reduction, including footers and retrievals. Not model billing or subscription quota.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "usagetrim_gain",
        "description": (
            "Local savings report by tool family from usagetrim telemetry "
            "(same data as `usagetrim gain`). Local output estimates only — "
            "not model billing or subscription quota."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "history": {
                    "type": "boolean",
                    "description": "Include recent per-event reductions in the JSON report.",
                },
                "passthrough": {
                    "type": "boolean",
                    "description": "When true, still returns the full report; passthrough ops are always listed.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 20,
                    "description": "History length when history is requested (default 20).",
                },
            },
        },
    },
]

_DEFAULT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
            },
        },
        "isError": {"type": "boolean"},
    },
    "required": ["content"],
}

for _tool in TOOLS_DEFINITIONS:
    if "outputSchema" not in _tool:
        _tool["outputSchema"] = _DEFAULT_OUTPUT_SCHEMA
    _tool["annotations"] = {
        "readOnlyHint": _tool["name"]
        not in {"usagetrim_exec", "usagetrim_context", "usagetrim_edit_symbol"},
        "destructiveHint": _tool["name"]
        in {"usagetrim_exec", "usagetrim_context", "usagetrim_edit_symbol"},
    }
    if _tool["name"] not in {
        "usagetrim_stats",
        "usagetrim_gain",
        "usagetrim_context",
        "usagetrim_edit_symbol",
        "usagetrim_clip",
        "usagetrim_pack",
        "usagetrim_distill",
        "usagetrim_table",
        "usagetrim_optimize",
    }:
        _tool["inputSchema"]["properties"]["max_tokens"] = {
            "type": "integer",
            "minimum": 64,
            "maximum": 32000,
            "default": 2000,
            "description": "Complete text budget using the local Claude estimate, including refs and exit status.",
        }
    if _tool["name"] in {"usagetrim_exec", "usagetrim_diff"}:
        _tool["inputSchema"]["properties"]["cwd"] = {
            "type": "string",
            "description": "Absolute working directory. Set explicitly for the target project.",
        }
    if _tool["name"] == "usagetrim_exec":
        _tool["inputSchema"]["properties"]["max_tokens"].pop("default")
        _tool["inputSchema"]["properties"]["max_tokens"]["description"] = (
            "Opt into truncation with this complete text budget (local estimate). "
            "Omit to preserve diagnostics and unknown output."
        )


CODING_TOOLS = frozenset(
    {
        "usagetrim_code",
        "usagetrim_edit_symbol",
        "usagetrim_exec",
        "usagetrim_read",
        "usagetrim_retrieve",
        "usagetrim_context",
        "usagetrim_diff",
        "usagetrim_stats",
        "usagetrim_gain",
    }
)

DESKTOP_TOOLS = frozenset(
    {
        "usagetrim_code",
        "usagetrim_edit_symbol",
        "usagetrim_exec",
        "usagetrim_read",
        "usagetrim_retrieve",
        "usagetrim_context",
        "usagetrim_diff",
        "usagetrim_stats",
        "usagetrim_gain",
        "usagetrim_optimize",
        "usagetrim_clip",
    }
)

DESKTOP_TOOL_DESCRIPTIONS = {
    "usagetrim_code": "Search local syntax index (symbols, occurrences, outline, callers, references, patterns) or ranked repo map.",
    "usagetrim_read": "Targeted file read by skeleton outline, symbol, or line range with bounded output.",
    "usagetrim_edit_symbol": "Atomic symbol replacement in source file guarded by file hash.",
    "usagetrim_exec": "Run noninteractive shell command with output compaction. Retains exit codes and diagnostics.",
    "usagetrim_diff": "Compact git diff folding lockfiles and build artifacts.",
    "usagetrim_retrieve": "Recover full original output from SQLite cache by ref_id.",
    "usagetrim_context": "Save/read bounded task milestones across session turns.",
    "usagetrim_stats": "Report session token savings.",
    "usagetrim_gain": "Local per-tool-family savings from telemetry. Estimates only — not billing or quota.",
    "usagetrim_optimize": "Autonomous prompt, code block, log, and table context optimizer.",
    "usagetrim_clip": "Compact noisy text, logs, diffs, or stack traces before pasting into chat.",
}


_COMPACT_PROP_DESCRIPTIONS = {
    "if_modified_since_hash": "Skip read if SHA-256 unchanged (returns 304).",
    "include_hash": "Prepend file SHA-256 for caching.",
    "strip_comments": "Strip comments and blank lines.",
    "skeleton": "Extract AST outline (classes/functions without bodies).",
    "auto_skeleton": "Progressively fold bodies if file exceeds budget.",
    "symbol": "Target symbol name (e.g. Class.method).",
    "expected_revision": "Revision expected before save.",
    "revision": "Specific revision to read.",
    "retention": "Task retention: ephemeral or permanent (save only).",
    "action": "Action: save, read, list, forget.",
    "task": "Task ID.",
    "checkpoint": "Checkpoint milestone data.",
    "mode": "Search mode (map, symbols, outline, callers, references).",
    "replacement": "Replacement source code.",
    "expected_hash": "Target file SHA-256 digest before edit.",
    "selector": "Qualified symbol selector.",
    "ref_id": "Recovery reference ID.",
    "max_tokens": "Text budget ceiling.",
    "budget": "Target token budget ceiling.",
    "root": "Absolute project root directory.",
    "path": "Target file or directory path.",
    "lines": "Target line range (e.g. '10-50').",
    "query": "Search query or pattern.",
    "command": "Shell command to execute.",
}


def _make_desktop_tool(base_tool: dict) -> dict:
    import copy

    tool = copy.deepcopy(base_tool)
    name = tool["name"]
    if name in DESKTOP_TOOL_DESCRIPTIONS:
        tool["description"] = DESKTOP_TOOL_DESCRIPTIONS[name]
    props = tool.get("inputSchema", {}).get("properties", {})
    for prop_name, prop_def in props.items():
        if prop_name in _COMPACT_PROP_DESCRIPTIONS and isinstance(prop_def, dict):
            prop_def["description"] = _COMPACT_PROP_DESCRIPTIONS[prop_name]
    return tool


def tool_definitions(profile: str = "full") -> list[dict]:
    """Return tool schemas filtered and formatted for the requested profile."""
    if profile not in {"full", "coding", "desktop"}:
        raise ValueError("MCP profile must be full, coding, or desktop")
    if profile == "full":
        return [tool for tool in TOOLS_DEFINITIONS]
    if profile == "desktop":
        allowed = DESKTOP_TOOLS
    else:
        allowed = CODING_TOOLS
    return [_make_desktop_tool(tool) for tool in TOOLS_DEFINITIONS if tool["name"] in allowed]


def server_instructions(profile: str) -> str:
    if profile == "desktop":
        return (
            "UsageTrim Desktop Profile (Claude Desktop & Codex Desktop). "
            "Use usagetrim_code for syntax searches; usagetrim_read for exact symbols or ranges; "
            "usagetrim_edit_symbol for hash-guarded edits; usagetrim_optimize/usagetrim_clip to shrink pasted logs, diffs and prompts; "
            "usagetrim_context for milestone checkpoints; usagetrim_exec for verbose commands; "
            "usagetrim_retrieve to recover full omitted output by ref. "
            "Keep normal approvals. This server does not intercept chat or change account quotas."
        )
    extra = (
        "Use usagetrim_optimize for unified autonomous prompt, code block, table, transcript, and log optimization; "
        "usagetrim_pack for file bundles, usagetrim_clip for pasted logs, "
        "usagetrim_distill for supplied transcripts, and usagetrim_table for tables. "
        if profile == "full"
        else "Optional text transforms remain available through the CLI. "
    )
    return (
        "Use usagetrim_code for syntax searches; usagetrim_read for exact symbols or ranges; "
        "usagetrim_edit_symbol for hash-guarded edits; usagetrim_context for milestone checkpoints. "
        + extra
        + "Use usagetrim_exec for verbose noninteractive commands with an absolute cwd. "
        "In Codex prefer usagetrim run inside the native shell to retain its sandbox and approvals. "
        "Omit max_tokens/max_lines to preserve command diagnostics; explicit limits permit truncation. "
        "Recover needed details with usagetrim_retrieve. Query local savings with usagetrim_gain "
        "(estimates only, not billing). Do not rerun successful commands just to compress output. "
        "Keep normal approvals. This server does not intercept chat or change account quotas."
    )


def _budget(arguments: dict[str, Any]) -> int:
    value = arguments.get("max_tokens", arguments.get("budget", 2000))
    if type(value) is not int or not 64 <= value <= 32000:
        raise ValueError("max_tokens must be an integer between 64 and 32000")
    return value


_START: ContextVar[float | None] = ContextVar("usagetrim_started", default=None)


def _timed(handler):
    @wraps(handler)
    def wrapped(arguments):
        token = _START.set(time.perf_counter())
        try:
            return handler(arguments)
        finally:
            _START.reset(token)

    return wrapped


def _record(raw: str, output: str, *, operation="exec", project=None, duration_s=None) -> str:
    global _SESSION_SAVED_CLAUDE, _SESSION_SAVED_OPENAI, _SESSION_SAVED_GEMINI
    before, after = count_tokens(raw), count_tokens(output)
    # Signed deltas expose expansion and charge subsequent retrievals in full.
    _SESSION_SAVED_CLAUDE += before.claude - after.claude
    _SESSION_SAVED_OPENAI += before.openai - after.openai
    _SESSION_SAVED_GEMINI += before.gemini - after.gemini
    if duration_s is None and _START.get() is not None:
        duration_s = time.perf_counter() - _START.get()
    record_text(
        raw,
        output,
        operation=operation,
        project=project,
        duration_s=duration_s,
        client=client_name("mcp"),
        engine="none" if paused() else "usagetrim",
    )
    return output


def _compress(text, budget, **kwargs):
    if paused():
        return kwargs.get("original_text", text) + kwargs.get("suffix", "")
    return compress_to_budget(text, budget, **kwargs)


def _cwd(arguments: dict[str, Any]) -> str | None:
    cwd = arguments.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        raise ValueError("cwd must be an absolute directory path")
    return cwd


@_timed
def handle_usagetrim_code(arguments: dict[str, Any]) -> str:
    from usagetrim.core.code_index import CodeIndex

    budget = _budget(arguments)
    index = CodeIndex(arguments["root"])
    result = index.query(
        arguments.get("mode", "map"),
        arguments.get("query", ""),
        arguments.get("file"),
        arguments.get("limit", 30),
    )
    output = _compress(result, budget, source="code-query")
    return _record(result, output, operation="code", project=arguments["root"])


@_timed
def handle_usagetrim_exec(arguments: dict[str, Any]) -> str:
    start = time.perf_counter()
    budget = _budget(arguments)
    command = arguments["command"]
    max_lines = arguments.get("max_lines", 80)
    if type(max_lines) is not int or not 1 <= max_lines <= 10000:
        raise ValueError("max_lines must be an integer between 1 and 10000")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a nonempty string")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=120,
            cwd=_cwd(arguments),
        )
        combined_raw = proc.stdout
        footer = f"\n[exit code: {proc.returncode}]"
    except subprocess.TimeoutExpired as exc:
        combined_raw = exc.stdout or ""
        if isinstance(combined_raw, bytes):
            combined_raw = combined_raw.decode("utf-8", errors="replace")
        footer = "\n[command timed out after 120s; output may be partial]"

    if already_wrapped(command):
        return combined_raw + footer
    if paused():
        output = combined_raw + footer
    elif any(key in arguments for key in ("max_tokens", "max_lines", "budget")):
        specialized = auto_specialize_command_output(command, combined_raw)
        base_text = specialized if specialized is not None else combined_raw
        opts = CleanerOptions(max_lines=max_lines, enable_cache=False)
        compacted = compact_terminal_output(base_text, opts)
        output = _compress(
            compacted,
            budget,
            original_text=combined_raw,
            suffix=footer,
            source="exec",
        )
    else:
        specialized = auto_specialize_command_output(command, combined_raw)
        if specialized is not None:
            output = specialized + footer
        else:
            output = safe_compact_output(combined_raw, command=command) + footer
    return _record(
        combined_raw, output, project=_cwd(arguments), duration_s=time.perf_counter() - start
    )


@_timed
def handle_usagetrim_read(arguments: dict[str, Any]) -> str:
    budget = _budget(arguments)
    path = arguments["path"]
    skeleton = arguments.get("skeleton", False)
    lines = arguments.get("lines")
    symbol = arguments.get("symbol")
    strip_comments = bool(arguments.get("strip_comments", False))
    include_hash = bool(arguments.get("include_hash", False))
    if_modified_since_hash = arguments.get("if_modified_since_hash")

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if if_modified_since_hash:
        source_bytes = p.read_bytes()
        current_hash = hashlib.sha256(source_bytes).hexdigest()
        clean_req = str(if_modified_since_hash).strip().lower()
        if (
            current_hash == clean_req
            or current_hash.startswith(clean_req)
            or clean_req.startswith(current_hash)
        ):
            notice = (
                f"# [usagetrim: 304 Not Modified. File '{p.name}' is unchanged "
                f"since hash {current_hash[:12]} ({len(source_bytes):,} bytes).]"
            )
            raw = p.read_text(encoding="utf-8", errors="replace")
            return _record(raw, notice, operation="read", project=path)

    from usagetrim.core.lockfile import is_lockfile, summarize_lockfile

    if is_lockfile(p.name) and lines is None:
        raw_full = p.read_text(encoding="utf-8", errors="replace")
        extracted = summarize_lockfile(raw_full, p.name, query_package=symbol)
        if include_hash:
            current_hash = hashlib.sha256(raw_full.encode("utf-8")).hexdigest()
            extracted = f"# [sha256: {current_hash[:16]}]\n" + extracted
        output = _compress(extracted, budget, source="read")
        return _record(raw_full, output, operation="read", project=path)

    auto_skeleton = bool(arguments.get("auto_skeleton", False))
    types_only = bool(arguments.get("types_only", False))

    extracted = extract_symbol_or_range(
        path,
        symbol=symbol,
        lines_range=lines,
        skeleton=skeleton,
        strip_comments=strip_comments,
        types_only=types_only,
    )
    if auto_skeleton and symbol is None and lines is None and not skeleton:
        if count_tokens(extracted).avg > budget:
            skel = extract_symbol_or_range(
                path,
                skeleton=True,
                strip_comments=strip_comments,
            )
            if skel and skel != extracted:
                notice = (
                    f"# [usagetrim: File exceeded {budget} token budget. Displaying structural outline. "
                    f"Use 'symbol' or 'lines' to read specific implementation.]\n"
                )
                extracted = notice + skel
    if include_hash:
        source_bytes = p.read_bytes()
        current_hash = hashlib.sha256(source_bytes).hexdigest()
        extracted = f"# [sha256: {current_hash[:16]}]\n" + extracted

    if not paused():
        viewed = ContextCache().session_view(extracted, source="read")
        if viewed != extracted:
            return _record(extracted, viewed, operation="read", project=path)

    output = _compress(extracted, budget, source="read")
    # Compare with the requested view, not an unrequested full-file read.
    return _record(extracted, output, operation="read", project=path)


@_timed
def handle_usagetrim_retrieve(arguments: dict[str, Any]) -> str:
    budget = _budget(arguments)
    ref_id = arguments["ref_id"]
    lines = arguments.get("lines")
    cache = ContextCache()
    query = arguments.get("query")
    if query is not None and lines is not None:
        raise ValueError("Choose query or lines, not both")
    retrieved = (
        cache.search(ref_id, query)
        if query is not None
        else cache.retrieve(ref_id, lines_range=lines)
    )
    if retrieved.startswith("Error:"):
        return retrieved
    output = _compress(retrieved, budget, source="retrieve")
    if recovery_engine(ref_id) == "rtk":
        record_text("", output, operation="retrieve", client=client_name("mcp"), engine="rtk")
        return output
    return _record("", output, operation="retrieve")


@_timed
def handle_usagetrim_diff(arguments: dict[str, Any]) -> str:
    budget = _budget(arguments)
    staged = arguments.get("staged", False)
    cmd = ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if staged:
        cmd.append("--cached")
    path_arg = arguments.get("path")
    if path_arg and isinstance(path_arg, str) and path_arg.strip():
        cmd.extend(["--", path_arg.strip()])
    res = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=30, cwd=_cwd(arguments)
    )
    if res.returncode:
        raise ValueError(f"git diff failed ({res.returncode}): {res.stderr[:500]}")
    raw_diff = res.stdout
    ignore_patterns = arguments.get("ignore_patterns")
    output = (
        _compress(
            budgeted_diff_slice(raw_diff, budget_tokens=budget, extra_patterns=ignore_patterns),
            budget,
            original_text=raw_diff,
            source="diff",
        )
        if raw_diff
        else "No git changes detected."
    )
    return _record(raw_diff, output, operation="diff", project=_cwd(arguments))


@_timed
def handle_usagetrim_tree(arguments: dict[str, Any]) -> str:
    budget = _budget(arguments)
    path_str = arguments.get("path", ".")
    max_depth = arguments.get("max_depth", 3)
    if type(max_depth) is not int or not 0 <= max_depth <= 64:
        raise ValueError("max_depth must be an integer between 0 and 64")
    target = Path(path_str).resolve()
    if not target.exists():
        raise ValueError(f"Path does not exist: {path_str}")

    root_node, _ = scan_directory(target, max_depth=max_depth)
    raw = format_tree_as_text(root_node, root_node.tokens)
    return _record(raw, _compress(raw, budget, source="tree"), operation="tree", project=target)


@_timed
def handle_usagetrim_json(arguments: dict[str, Any]) -> str:
    budget = _budget(arguments)
    max_items = arguments.get("max_items", 3)
    if type(max_items) is not int or max_items < 0:
        raise ValueError("max_items must be a nonnegative integer")
    raw_str = arguments.get("json_str")
    path_str = arguments.get("path")
    if path_str:
        raw_str = Path(path_str).read_text(encoding="utf-8", errors="replace")

    if not raw_str:
        return "Error: either json_str or path must be provided."

    preview = slim_json(raw_str, max_array_items=max_items, cache_full=False)
    output = _compress(preview, budget, original_text=raw_str, source="json")
    return _record(raw_str, output, operation="json", project=path_str)


def handle_usagetrim_stats() -> str:
    return (
        f"usagetrim Session Savings:\n"
        f"Estimated net text reduction (negative means overhead):\n"
        f"- Claude heuristic: {_SESSION_SAVED_CLAUDE:,}\n"
        f"- OpenAI o200k estimate: {_SESSION_SAVED_OPENAI:,}\n"
        f"- Gemini heuristic: {_SESSION_SAVED_GEMINI:,}\n"
        "Includes exit/ref text and retrievals; excludes schemas, JSON envelopes, prompts, and reasoning.\n"
        "Not billing, model-specific token counts, or subscription quota savings.\n"
    )


def handle_usagetrim_gain(arguments: dict[str, Any] | None = None) -> str:
    """Return durable local savings JSON (not this-process session counters)."""
    arguments = arguments or {}
    limit = arguments.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    # history/passthrough flags mirror the CLI view toggles; JSON always carries
    # by_operation + passthrough. Omit history rows unless asked to keep payloads small.
    include_history = bool(arguments.get("history", False))
    report = build_gain_report(history_limit=limit if include_history else 1)
    payload = report.to_dict()
    if not include_history:
        payload["history"] = []
    return json.dumps(payload, indent=2)


def _validate_arguments(name: str, arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    schema = next(tool["inputSchema"] for tool in TOOLS_DEFINITIONS if tool["name"] == name)
    for required in schema.get("required", []):
        if required not in arguments:
            raise ValueError(f"Missing required argument: {required}")
    types = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}
    for key, value in arguments.items():
        spec = schema["properties"].get(key)
        if spec and type(value) is not types[spec["type"]]:
            raise ValueError(f"{key} must be {spec['type']}")


@_timed
def handle_usagetrim_clip(arguments: dict[str, Any]) -> str:
    from usagetrim.core.clip import compact_text

    text = arguments.get("text", "")
    budget = arguments.get("budget", 2000)
    if type(budget) is not int or not 64 <= budget <= 32000:
        raise ValueError("budget must be an integer between 64 and 32000")
    res = compact_text(text, budget=budget)
    return _record(text, res.text, operation="clip")


@_timed
def handle_usagetrim_pack(arguments: dict[str, Any]) -> str:
    from usagetrim.core.pack import pack_context

    root_str = arguments.get("root")
    if not root_str:
        raise ValueError("root is required")
    root = Path(root_str).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be a valid directory: {root_str}")
    paths = arguments.get("paths")
    budget = arguments.get("budget", 4000)
    if type(budget) is not int or not 100 <= budget <= 64000:
        raise ValueError("budget must be an integer between 100 and 64000")
    force_skeleton = bool(arguments.get("skeleton", False))

    res = pack_context(
        paths=paths,
        root=root,
        budget=budget,
        force_skeleton=force_skeleton,
    )
    return _record("", res.bundle_text, operation="pack", project=root)


@_timed
def handle_usagetrim_distill(arguments: dict[str, Any]) -> str:
    from usagetrim.core.distill import distill_conversation

    transcript = arguments.get("transcript", "")
    budget = arguments.get("budget", 1500)
    if type(budget) is not int or not 100 <= budget <= 32000:
        raise ValueError("budget must be an integer between 100 and 32000")
    res = distill_conversation(transcript, budget=budget)
    return _record(transcript, res.text, operation="distill")


@_timed
def handle_usagetrim_table(arguments: dict[str, Any]) -> str:
    from usagetrim.core.table import compact_table

    data = arguments.get("data", "")
    budget = arguments.get("budget", 2000)
    format_type = arguments.get("format", "toon")
    if type(budget) is not int or not 100 <= budget <= 32000:
        raise ValueError("budget must be an integer between 100 and 32000")
    if format_type not in {"toon", "markdown"}:
        raise ValueError("format must be 'toon' or 'markdown'")
    res = compact_table(data, budget=budget, format_type=format_type)
    return _record(data, res.text, operation="table")


@_timed
def handle_usagetrim_optimize(arguments: dict[str, Any]) -> str:
    from usagetrim.core.optimizer import optimize_context

    content = arguments.get("content", "")
    budget = arguments.get("budget", 2000)
    if type(budget) is not int or not 64 <= budget <= 64000:
        raise ValueError("budget must be an integer between 64 and 64000")
    res = optimize_context(content, budget=budget)
    return _record(content, res.text, operation="optimize")


def handle_usagetrim_context(arguments: dict[str, Any]) -> str:
    from usagetrim.core.task_context import dispatch_context

    output = json.dumps(dispatch_context(arguments), ensure_ascii=False)
    return _record("", output, operation="context", project=arguments.get("root"))


def handle_usagetrim_edit_symbol(arguments: dict[str, Any]) -> str:
    from usagetrim.core.symbol_edit import replace_symbol

    path_str = arguments.get("path")
    if not path_str:
        raise ValueError("path is required")
    selector = arguments.get("selector")
    if not selector:
        raise ValueError("selector is required")
    replacement = arguments.get("replacement")
    if replacement is None:
        raise ValueError("replacement is required")
    expected_hash = arguments.get("expected_hash")
    if not expected_hash:
        raise ValueError("expected_hash is required")
    apply = bool(arguments.get("apply", False))

    path = Path(path_str).resolve()
    result = replace_symbol(path, selector, replacement, expected_hash, apply=apply)
    output = redact_secrets(result)
    return _record("", output, operation="edit_symbol", project=path)


def _respond(req: Any, *, profile: str = "full") -> dict[str, Any] | None:
    if (
        not isinstance(req, dict)
        or req.get("jsonrpc") != "2.0"
        or not isinstance(req.get("method"), str)
    ):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid request"},
        }
    # JSON-RPC notifications never receive a response or execute tool calls.
    if "id" not in req:
        return None
    response = {"jsonrpc": "2.0", "id": req["id"]}
    method, params = req["method"], req.get("params", {})
    if not isinstance(params, dict):
        return {**response, "error": {"code": -32602, "message": "params must be an object"}}
    if method == "initialize":
        return {
            **response,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "usagetrim", "version": __version__},
                "instructions": server_instructions(profile),
            },
        }
    if method == "tools/list":
        return {**response, "result": {"tools": tool_definitions(profile)}}
    if method == "ping":
        return {**response, "result": {}}
    if method != "tools/call":
        return {**response, "error": {"code": -32601, "message": "Method not found"}}
    handlers = {
        "usagetrim_context": handle_usagetrim_context,
        "usagetrim_code": handle_usagetrim_code,
        "usagetrim_edit_symbol": handle_usagetrim_edit_symbol,
        "usagetrim_exec": handle_usagetrim_exec,
        "usagetrim_read": handle_usagetrim_read,
        "usagetrim_retrieve": handle_usagetrim_retrieve,
        "usagetrim_diff": handle_usagetrim_diff,
        "usagetrim_tree": handle_usagetrim_tree,
        "usagetrim_json": handle_usagetrim_json,
        "usagetrim_clip": handle_usagetrim_clip,
        "usagetrim_pack": handle_usagetrim_pack,
        "usagetrim_distill": handle_usagetrim_distill,
        "usagetrim_table": handle_usagetrim_table,
        "usagetrim_optimize": handle_usagetrim_optimize,
        "usagetrim_stats": lambda _: handle_usagetrim_stats(),
        "usagetrim_gain": handle_usagetrim_gain,
    }
    name, arguments = params.get("name"), params.get("arguments", {})
    if not isinstance(name, str) or name not in {
        tool["name"] for tool in tool_definitions(profile)
    }:
        return {**response, "error": {"code": -32602, "message": "Unknown tool"}}
    try:
        _validate_arguments(name, arguments)
        output = handlers[name](arguments)
        is_error = output.startswith("Error:")
    except Exception as exc:
        output, is_error = f"Error: {redact_secrets(str(exc))[:500]}", True
    return {
        **response,
        "result": {"content": [{"type": "text", "text": output}], "isError": is_error},
    }


def run_mcp_stdio_server(profile: str = "full"):
    """Run JSON-RPC 2.0 stdio loop for Model Context Protocol."""
    tool_definitions(profile)  # Validate before reading stdin or executing anything.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            resp = _respond(req, profile=profile)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
