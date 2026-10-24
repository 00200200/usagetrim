# Contributing to UsageTrim

UsageTrim compacts tool output for coding assistants while keeping the original
available by reference. Useful contributions include reproductions of lost
diagnostic context, command filters, regression tests, and integration fixes.
Check existing issues and pull requests before starting overlapping work.

---

## Development setup

Use Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) for the development environment.

### 1. Clone & Install
```bash
git clone https://github.com/00200200/usagetrim.git
cd usagetrim

# Sync dependencies and virtual environment
uv sync --all-extras
```

### 2. Run Tests
```bash
# Run the complete test suite
uv run pytest -v

# Run with test coverage
uv run pytest --cov=usagetrim
```

### 3. Lint & Format
```bash
# Check code with Ruff
uv run ruff check .

# Auto-format code
uv run ruff format .
```

---

## Code layout

Before writing code, here is where things live in `src/usagetrim/`:

```
src/usagetrim/
├── cli.py                     # CLI commands (run, cat, retrieve, tree, diff, lint, hook, demo)
├── core/
│   ├── cleaner.py             # Log compactor, ANSI stripper, error & stacktrace preserver
│   ├── adaptive.py            # Hard budget enforcement (--budget <N>) with binary search slicing
│   ├── cache.py               # SQLite-backed Compress-Cache-Retrieve (CCR) store
│   ├── specialized.py         # Context-aware filters for git log, git status, test runners
│   ├── skeleton.py            # AST & regex structural outline generator (Py, TS, Go, Rust)
│   ├── diff_slimmer.py        # Git diff compactor and lockfile folding
│   ├── redactor.py            # Sensitive credential & API key masking
│   ├── tree_scanner.py        # Directory token distribution scanner & tree visualizer
│   ├── rules_linter.py        # CLAUDE.md / .cursorrules prompt cache linter & minifier
│   └── hooks.py               # Shell aliases and Claude Code hook installer
├── mcp/
│   └── server.py              # Stdio JSON-RPC 2.0 Model Context Protocol server
└── metrics/
    ├── tokenizer.py           # Local token counts and estimates
    └── pricing.py             # Cost estimates, not billing measurements
```

---

## Adding capabilities

### Adding a New Specialized Command Filter
If you want to optimize a specific command (e.g. `docker ps`, `cargo test`, `kubectl`):
1. Open `src/usagetrim/core/specialized.py`.
2. Add a specialized filter function: `def filter_my_command(raw: str) -> str`.
3. Wire it into `auto_specialize_command_output(command, raw_output)`.
4. Add unit tests in `tests/test_adaptive_and_specialized.py`.

### Adding AST Skeletonizer for a New Language
If you want to add language support (e.g. C++, Java, Kotlin, Swift):
1. Open `src/usagetrim/core/skeleton.py`.
2. Update `_skeletonize_by_regex` or add a specialized parser for the extension.
3. Test that method bodies `{ ... }` are collapsed while signatures and docstrings are preserved.
4. Add unit test in `tests/test_skeleton.py`.

### Adding a New MCP Tool
1. Open `src/usagetrim/mcp/server.py`.
2. Add tool specification to `TOOLS_DEFINITIONS`.
3. Add handler function: `handle_my_tool(arguments)`.
4. Wire it into `tools/call` dispatcher in `run_mcp_stdio_server()`.
5. Add test in `tests/test_mcp_server.py`.

---

## Pull request guidelines

1. **Keep Code Clean:** Run `uv run ruff check .` and `uv run ruff format .` before pushing.
2. **Add Tests:** Every new feature or bugfix should include corresponding unit tests in `tests/`.
3. **No Breaking Quality Loss:** Ensure errors and tracebacks are never suppressed. The AI model must always receive actionable error information.
4. **Conventional Commits:** Use standard prefixes:
   - `feat: ...` for new features
   - `fix: ...` for bugfixes
   - `perf: ...` for performance/compression improvements
   - `docs: ...` for documentation
   - `test: ...` for tests

Include the checks you ran and any platforms or integrations you did not test.
