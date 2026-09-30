<p align="center">
  <picture>
    <source media="(prefers-reduced-motion: reduce)" srcset="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/readme-hero.png">
    <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/readme-hero.gif" width="100%" alt="UsageTrim: keep the signal, cut the noise. Authored fixtures cut docker 18,360→107, cargo 3,795→185, pytest 1,562→174.">
  </picture>
</p>

<p align="center">
  <strong>Keep the signal. Cut the noise.</strong><br>
  Local CLI + MCP that folds verbose tool output for Claude Code, Codex, Cursor, and Claude Desktop — recoverable by reference. Optional macOS pet.
</p>

<p align="center">
  <a href="https://github.com/00200200/usagetrim/actions/workflows/ci.yml"><img src="https://github.com/00200200/usagetrim/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/00200200/usagetrim/releases"><img src="https://img.shields.io/github/v/release/00200200/usagetrim?include_prereleases&amp;color=b3f5cd&amp;label=release" alt="Release"></a>
  <a href="https://github.com/00200200/usagetrim/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-b3f5cd" alt="MIT license"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-compatible-2ec79b" alt="MCP Compatible"></a>
  <img src="https://img.shields.io/badge/runs%20in-Claude%20Code%20·%20Codex%20·%20Cursor%20·%20Desktop-1a3330" alt="Runs in Claude Code, Codex, Cursor, Desktop">
  <a href="https://github.com/00200200/usagetrim/stargazers"><img src="https://img.shields.io/github/stars/00200200/usagetrim?style=social" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="https://00200200.github.io/usagetrim/">Website</a> ·
  <a href="#install">Install</a> ·
  <a href="#see-it-cut">Demo</a> ·
  <a href="#what-you-get">Features</a> ·
  <a href="#connect-your-agent">Connect</a> ·
  <a href="#desktop-companion">Pet</a> ·
  <a href="https://github.com/00200200/usagetrim/blob/main/docs/guide.md">Guide</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/harnesses.svg" width="920" alt="Harnesses and tools: Claude, Codex, Cursor, Desktop, pytest, docker, cargo, go, eslint, mypy">
</p>

---

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) (pipx works too).

```sh
uv tool install git+https://github.com/00200200/usagetrim
usagetrim demo
```

Try without installing: `uvx --from git+https://github.com/00200200/usagetrim usagetrim demo`.

### Add it inside your app

| App | Install |
|---|---|
| **Claude Code** | `/plugin marketplace add 00200200/usagetrim`, then `/plugin install usagetrim@usagetrim` — MCP tools, Bash and MCP output hooks, Haiku agents |
| **Claude Desktop** | Customize → Plugins → add the marketplace `00200200/usagetrim`, or double-click `usagetrim-<version>.mcpb` from [Releases](https://github.com/00200200/usagetrim/releases) (Settings → Extensions) |
| **Codex app & CLI** | `codex plugin marketplace add 00200200/usagetrim`, then `codex plugin add usagetrim@usagetrim` (or pick it under Plugins in the app) |
| **Cursor, Windsurf** | `usagetrim install --cursor` or `usagetrim install --windsurf` |

Plugins run the pinned release wheel through `uvx`, so they need [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`. The `.mcpb` extension installs its own copy through Claude Desktop.

```
verbose tool text  →  keep the failure  →  recover the rest by reference
```

---

## See it cut

`usagetrim demo` is offline — **no model calls**. Failures stay; originals recover exactly.

<p align="center">
  <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/cuts.svg" width="920" alt="Measured savings: docker 99.4%, go 98%, terraform 96%, cargo 95.1%, kubectl 92.5%">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/demo.svg" width="860" alt="Terminal showing usagetrim demo measured fixture results">
</p>

| Fixture | Tokens |
| --- | ---: |
| `docker build` BuildKit | 18,360 → **107** (99.4%) |
| `go test` + goroutine dump | 3,281 → **64** (98.0%) |
| `terraform plan` refresh/read | 5,885 → **236** (96.0%) |
| `cargo test` + backtrace | 3,795 → **185** (95.1%) |
| pytest noisy (xdist + I/O) | 5,111 → **349** (93.2%) |
| `kubectl describe` pod | 8,049 → **601** (92.5%) |
| pytest recovery demo | 1,562 → **174** (88.9%) |
| `vitest` / `eslint` / `tsc` | up to **98.8%** / **79%** |

Local `o200k_base` estimate — not billing, quality, or subscription-limit claims. Full matrix: `usagetrim demo --json`.

```sh
usagetrim run -- pytest -v
usagetrim run -- docker build -t app .
usagetrim run -- cargo test
usagetrim run -- go test ./...
usagetrim run -- kubectl describe pod api-7d8f9c-xk2m9
usagetrim run -- terraform plan
usagetrim run -- npx eslint . --format codeframe
```

---

## What you get

- **Compact other MCP servers, losslessly.** The Claude Code hook rewrites results from *other* MCP servers: uniform JSON rows become TSV with the keys once, `{"result": "…"}` wrappers lose their escaping, and indentation goes. Every value and type survives, and prompt-injection boundaries such as Supabase's `<untrusted-data-…>` stay verbatim. [Measured below](#measured-on-real-sessions).
- **Spend Haiku, not Opus, on reading.** The plugin adds `scout` (read-only search) and `runner` (tests and builds, failures only) agents on Haiku, so the main model gets conclusions instead of files and logs. `usagetrim install --cheap-explore` moves Claude Code's built-in Explore, which now inherits the main model, back to Haiku. `/output-style usagetrim:lean` trims reply preambles and recaps.
- **Cut noise, keep the failure.** Specialized filters for pytest, Docker, cargo, go, vitest, eslint, tsc, mypy, pyright, kubectl, terraform, GitHub Actions / GitLab CI log folding (`gh run view --log-failed`, `glab ci trace`), `uv sync`/`uv add`, git diff, ruff…
- **Session dedup + spill.** Same `run` output *or* identical `cat` / MCP `usagetrim_read` view within ~15 minutes → short cache ref. Payloads over ~20 KiB → file + preview (`USAGETRIM_SPILL_BYTES`).
- **Recover by reference.** Omitted text stays in a local CCR cache: `usagetrim retrieve tc_…`
- **Measure it.** `usagetrim gain` / MCP `usagetrim_gain` — per-tool-family savings and passthrough candidates (local estimates, not account quotas).
- **Desktop-ready.** MCP for Claude Code / Codex / Cursor / Claude Desktop; Prepare-for-chat clipboard flow; optional macOS pet.

```sh
usagetrim gain                 # summary + by tool family + passthrough tips
usagetrim gain --history       # same tables + recent Raw→Compact / Saved rows
usagetrim gain --passthrough   # near-zero cuts only (specializer candidates)
usagetrim prepare --file draft.txt
```

[Details and tradeoffs →](https://github.com/00200200/usagetrim/blob/main/docs/guide.md)

### Measured on real sessions

14 days of the maintainer's Claude Code transcripts, replayed offline through the same functions the hook runs. Token counts are local `o200k_base` estimates, not billing.

| Tool results | Count | Tokens before → after | Cut |
|---|---:|---:|---:|
| All third-party MCP results | 11,058 | 7.17M → 5.85M | **18.3%** |
| Supabase `execute_sql` | 6,571 | 4.97M → 4.03M | 18.9% |
| Google Search Console analytics | 223 | 702k → 434k | 38.2% |
| Vercel `list_deployments` | 43 | 250k → 210k | 16.1% |

All 3,144 SQL results rewritten as TSV decoded back to the original rows ([reproduce on your own transcripts](https://github.com/00200200/usagetrim/blob/main/scripts/replay_mcp_transcripts.py): `uv run python scripts/replay_mcp_transcripts.py`). Browser and page-text tools return prose and were left alone. Bash output in the same sessions was mostly ad-hoc scripts, where the filters saved under 1%. Test and build logs are where the Bash filters pay off, so results depend on what your tools print.

---

## How it fits

UsageTrim is **not** a chat interceptor. It sits on the tool path (CLI wrapper, MCP, Prepare-for-chat) so agents still see failures — just without the noise.

| Approach | What UsageTrim does instead |
| --- | --- |
| Blind head/tail truncation | Specialized cutters keep the failure signal; rest recovers via `usagetrim retrieve` |
| Rewrite the whole chat stream | MCP + `prepare` only — Desktop cannot rewrite model turns |
| Opaque “saved tokens” badges | `usagetrim gain` shows local Raw→Compact by tool family (not billing quotas) |

Same CCR idea as peers (compress → cache → retrieve). Differentiation is specialized cutters, session dedup, spill-to-file, and Desktop/MCP install paths — not a claim that we beat RTK/snip/headroom on every workload.

---

## Connect your agent

**Desktop & Coding profiles:** 9–11 essential tools instead of 16 — about **36–38% smaller tool schemas** in local `o200k_base` measurements (3,414 → 2,183 / 2,102). Not a per-turn usage guarantee.

```sh
# One-command installer for Codex & Claude Desktop
usagetrim install --codex            # configures ~/.codex/config.toml
usagetrim install --claude-desktop   # configures Claude Desktop MCP
usagetrim install --mcpb             # Extension manifest for one-click packaging
usagetrim install --all              # configures all at once
```

For Claude Code CLI:
```sh
claude mcp add --scope user usagetrim -- usagetrim mcp --profile coding
# or, without installing first:
claude mcp add --scope user usagetrim -- uvx --from git+https://github.com/00200200/usagetrim usagetrim mcp
```

Manual MCP configuration for Claude Desktop, Cursor, Codex, Windsurf:

```json
{
  "mcpServers": {
    "usagetrim": {
      "command": "/absolute/path/to/usagetrim",
      "args": ["mcp", "--profile", "desktop"]
    }
  }
}
```

Path: `command -v usagetrim`. Use `--profile full` for every tool, `--profile coding` for core terminal tools, or `--profile desktop` for chat apps. [Client setup →](https://github.com/00200200/usagetrim/blob/main/docs/guide.md#connect-clients)

```sh
# Prepare messy logs or stack traces with prompt-cache prefix stabilization:
usagetrim prepare --desktop -f error.log

# Initialize or optimize lean, cache-aligned instructions (CLAUDE.md / AGENTS.md):
usagetrim rules --init --client claude        # writes lean CLAUDE.md (~120 tokens)
usagetrim rules --init --client codex         # writes lean AGENTS.md (~120 tokens)
usagetrim rules --optimize --write -f CLAUDE.md # strips filler, aligns prompt caching
```

---

## Desktop companion

Mint robot on your Mac — draggable pet or menu-bar mode. Local measurements, task memory, optional account-limit readings. English UI. No extra AI calls.

<p align="center">
  <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/macos-pet.png" width="640" alt="UsageTrim macOS pet with example five-hour balances: Codex 68% left, Claude 42% left.">
</p>

*Example balances are remaining allowance — not savings caused by UsageTrim.*

<p align="center">
  <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/macos-conversation.png" width="720" alt="Prepare for chat window comparing an authored conversation with a local preview">
</p>

*Prepare for chat — paste a log, preview the cut, copy into Codex / Claude Desktop. [Log prep →](https://raw.githubusercontent.com/00200200/usagetrim/main/assets/macos-prepare.png)*

**macOS 13+ · ad-hoc signed, not notarized.** CLI and MCP work without the pet. [Build →](https://github.com/00200200/usagetrim/blob/main/docs/guide.md#macos-companion)

---

## Roadmap

Ideas from the strongest tools in this space, adopted only with a measurement behind them:

- Ranked repo map (Aider-style PageRank over the symbol graph) fitted to a token budget.
- LSP-backed references and rename (Serena-style), falling back to the ast-grep index.
- Lossless log templating (Drain-style) for dev-server, Docker and CI logs.
- A paired A/B harness on provider-reported tokens and task pass rate, because output-size savings alone can hide extra turns.

## Make it better with us

If UsageTrim earns a place in your workflow, **[star the repo](https://github.com/00200200/usagetrim/stargazers)**. Lost context or a missed cut? [Open an issue](https://github.com/00200200/usagetrim/issues/new) with a small redacted example.

<p align="center">
  <a href="https://star-history.com/#00200200/usagetrim&amp;Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=00200200/usagetrim&amp;type=Date&amp;theme=dark" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=00200200/usagetrim&amp;type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=00200200/usagetrim&amp;type=Date" width="600" />
    </picture>
  </a>
</p>

[Guide](https://github.com/00200200/usagetrim/blob/main/docs/guide.md) · [Measurements](https://github.com/00200200/usagetrim/blob/main/docs/guide.md#measurements-and-development) · [MIT](https://github.com/00200200/usagetrim/blob/main/LICENSE)

<!-- mcp-name: io.github.00200200/usagetrim -->
