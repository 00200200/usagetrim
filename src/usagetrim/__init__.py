"""usagetrim — Context compression engine and MCP server for Claude Code, Cursor, and Gemini CLI."""

__version__ = "0.4.0"

from usagetrim.core.cache import ContextCache
from usagetrim.core.cleaner import CleanerOptions, compact_terminal_output, strip_ansi
from usagetrim.core.config import UsagetrimConfig, load_config
from usagetrim.core.diff_slimmer import budgeted_diff_slice, slim_git_diff
from usagetrim.core.doctor import run_all_diagnostics
from usagetrim.core.json_slimmer import slim_json
from usagetrim.core.optimizer import OptimizeResult, optimize_context
from usagetrim.core.pr_analyzer import PRTokenReport, analyze_pr_tokens
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.skeleton import (
    extract_symbol_or_range,
    skeletonize_code_ast,
    skeletonize_python,
)
from usagetrim.core.tree_scanner import scan_directory
from usagetrim.metrics.pricing import CostSavings, estimate_savings
from usagetrim.metrics.tokenizer import (
    TokenCount,
    TokenReductionMetrics,
    compute_metrics,
    count_tokens,
)

__all__ = [
    "CleanerOptions",
    "CostSavings",
    "ContextCache",
    "OptimizeResult",
    "PRTokenReport",
    "TokenCount",
    "TokenReductionMetrics",
    "UsagetrimConfig",
    "analyze_pr_tokens",
    "budgeted_diff_slice",
    "compact_terminal_output",
    "compute_metrics",
    "count_tokens",
    "estimate_savings",
    "extract_symbol_or_range",
    "load_config",
    "optimize_context",
    "redact_secrets",
    "run_all_diagnostics",
    "scan_directory",
    "skeletonize_code_ast",
    "skeletonize_python",
    "slim_git_diff",
    "slim_json",
    "strip_ansi",
]
