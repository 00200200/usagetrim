# UsageTrim

A local CLI and MCP server for compacting verbose test and build output.
Filters use format-specific rules to retain diagnostic context; cached output is
recoverable after recognized secrets are redacted, while the entry remains stored.
Integrations are available for Claude Code, Codex, Cursor,
and Claude Desktop; the macOS companion is optional.

<p align="center">
  <a href="https://github.com/00200200/usagetrim/actions/workflows/ci.yml"><img src="https://github.com/00200200/usagetrim/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/00200200/usagetrim/releases"><img src="https://img.shields.io/github/v/release/00200200/usagetrim?include_prereleases&amp;color=b3f5cd&amp;label=release" alt="Release"></a>
  <a href="https://github.com/00200200/usagetrim/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-b3f5cd" alt="MIT license"></a>
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

`usagetrim demo` runs authored fixtures with **no model calls**. It checks failure retention and exact recovery for those fixtures, which contain no recognized secrets. These checks do not guarantee preservation of every diagnostic in arbitrary output.

<p align="center">
  <img src="https://raw.githubusercontent.com/00200200/usagetrim/main/assets/demo.svg" width="860" alt="Terminal showing usagetrim demo measured fixture results">
</p>

| Fixture | Tokens |
| --- | ---: |
| `docker build` BuildKit | 18,360 → **107** (99.4%) |
| `go test` + goroutine dump | 3,281 → **64** (98.0%) |
| `terraform plan` refresh/read | 6,409 → **276** (95.7%) |
| `cargo test` + backtrace | 3,795 → **185** (95.1%) |
| pytest noisy (xdist + I/O) | 5,111 → **349** (93.2%) |
| `kubectl describe` pod | 8,127 → **595** (92.7%) |
| pytest recovery demo | 1,562 → **174** (88.9%) |
| `vitest` / `eslint` / `tsc` | up to **98.8%** / **79%** |

Local `o200k_base` estimate — not billing, quality, or subscription-limit claims. Full matrix: `usagetrim demo --json`.

```sh
usagetrim run -- pytest -v
usagetrim run -- docker build -t app .
usagetrim run -- docker compose logs
usagetrim run -- cargo test
usagetrim run -- go test ./...
usagetrim run -- kubectl describe pod api-7d8f9c-xk2m9
usagetrim run -- terraform plan
usagetrim run -- npx eslint . --format codeframe
```

---

## What you get

- **Compact supported MCP text formats.** The Claude Code hook rewrites results from *other* MCP servers: uniform JSON rows become TSV with the keys once, `{"result": "…"}` wrappers lose their escaping, and indentation goes. The rewrites target supported JSON shapes and preserve recognized `<untrusted-data-…>` delimiters and surrounding warnings; they do not provide a prompt-injection defense. [Measured below](#measured-on-real-sessions).
- **Focused helper agents.** The plugin adds `scout` (read-only search) and `runner` (tests and builds, failures only) agents on Haiku. `usagetrim install --cheap-explore` configures Claude Code's built-in Explore to use Haiku. `/output-style usagetrim:lean` trims reply preambles and recaps.
- **Compose logs.** `docker compose` / `docker-compose` / `podman compose` / `podman-compose` `up` and `logs` fold repeated successful healthchecks, HTTP health polls and Redis heartbeats per service. Unknown lines, errors and exit events remain intact. Before every exit, the last ten lines of every observed service are preserved: even an exact name match can refer to another service's container. This deliberately favors crash context over maximum compression when project or custom container names are ambiguous. ANSI-decorated or other unrecognized formats are left unchanged; no Docker/Podman daemon is needed by the filter.
- **Cut noise, keep the failure.** Specialized filters for pytest, Docker, cargo, go, vitest, eslint, tsc, mypy, pyright, kubectl, terraform/tofu plan|apply|destroy, GitHub Actions / GitLab CI log folding (`gh run view --log-failed`, `glab ci trace`), `uv sync`/`uv add`, git diff, ruff…
- **Session dedup + spill.** Same `run` output *or* identical `cat` / MCP `usagetrim_read` view within ~15 minutes → short cache ref. Payloads over ~20 KiB → file + preview (`USAGETRIM_SPILL_BYTES`).
- **Recover by reference.** `usagetrim retrieve tc_…` returns cached text after recognized secrets are redacted. Cache entries can be evicted when the size budget is exceeded, pruned, or cleared; references are not permanent backups. Redaction uses patterns and may miss sensitive content.
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

Maintainer-reported results from 14 days of Claude Code transcripts, replayed offline through the same functions the hook runs. Token counts are local `o200k_base` estimates, not billing; the private transcripts are not included as a public benchmark dataset.

| Tool results | Count | Tokens before → after | Cut |
|---|---:|---:|---:|
| All third-party MCP results | 11,058 | 7.17M → 5.85M | **18.3%** |
| Supabase `execute_sql` | 6,571 | 4.97M → 4.03M | 18.9% |
| Google Search Console analytics | 223 | 702k → 434k | 38.2% |
| Vercel `list_deployments` | 43 | 250k → 210k | 16.1% |

All 3,144 SQL results rewritten as TSV decoded back to the original rows ([reproduce on your own transcripts](https://github.com/00200200/usagetrim/blob/main/scripts/replay_mcp_transcripts.py): `uv run python scripts/replay_mcp_transcripts.py`). Browser and page-text tools return prose and were left alone. Bash output in the same sessions was mostly ad-hoc scripts, where the filters saved under 1%. Test and build logs are where the Bash filters pay off, so results depend on what your tools print.

---

## How it fits

UsageTrim sits on the tool path (CLI wrapper, MCP, Prepare-for-chat). It does not intercept or rewrite the chat stream. Its filters aim to retain failure details while reducing repetitive output; inspect the cached text when the compact view is insufficient.

| Approach | What UsageTrim does instead |
| --- | --- |
| Blind head/tail truncation | Specialized cutters retain recognized failure details; redacted cached text is available via `usagetrim retrieve` while retained |
| Rewrite the whole chat stream | MCP + `prepare` only — Desktop cannot rewrite model turns |
| Opaque “saved tokens” badges | `usagetrim gain` shows local Raw→Compact by tool family (not billing quotas) |

Same CCR idea as peers (compress → cache → retrieve). Differentiation is specialized cutters, session dedup, spill-to-file, and Desktop/MCP install paths — not a claim that we beat RTK/snip/headroom on every workload.

---

## Connect your agent

**Desktop & Coding profiles:** 11 and 9 tools respectively, compared with 16 in the full profile, with shorter schema descriptions. Schema token counts depend on the release, serialization, and tokenizer; smaller schemas do not guarantee lower per-turn usage.

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

**Linux / Windows MVP:** install `pip install 'usagetrim[companion]'`, then run
`usagetrim companion` in a graphical desktop session. The optional PySide6 window
shows today's actual monitor aggregates (operations, raw/compact token estimates,
and signed net reduction), refreshes every five seconds and can pause/resume
optimization using the same local settings as CLI/MCP. An empty history is shown
as unavailable measurements, not invented savings. After a refresh failure, pause
controls remain disabled until a successful read restores the current state.
Closing the window immediately stops UI updates and cancels queued monitor work
without waiting on the GUI thread. A monitor call already running is allowed to finish;
Python may wait for it before the process exits. This MVP cannot guarantee an
immediate process exit if local filesystem or database operations stall. It does
not forcibly terminate threads or apply their results to a closed window.

On Windows, install and launch from PowerShell (the module form does not require
the Python Scripts directory on `PATH`):

```powershell
py -3.12 -m pip install "usagetrim[companion]"
py -3.12 -m usagetrim.cli companion
```

Drag the standard title bar to move the window. The opaque, normally decorated
window works through Qt's native Windows, X11 or Wayland support; a working Qt
platform plugin and system GUI libraries are required. The optional always-on-top
hint is saved in `companion.json`, but compositors may ignore it (especially on
Wayland); the normal window remains usable.

Use **Desktop Pet** to show the bundled Mint robot (the same artwork as macOS),
and **Statistics panel** to return. Drag Mint or the standard title bar. Pet mode
uses a frameless transparent window only when Windows composition or an X11
compositor can be confirmed. Otherwise it keeps normal window decorations and an
opaque background, including on Wayland. On Wayland global coordinates are not
saved or restored; placement and dragging remain under compositor control.
Mode and supported Pet coordinates are stored in the existing `companion.json`;
restored positions are clamped to an available screen. Preference writes use an
independent, serialized worker and are coalesced during dragging. Normal closing
captures the final mode and position even before the drag timer fires, and drains
the final atomic settings write without waiting for a monitor read in the GUI.
Normal Python process shutdown waits for that settings worker. This does not
guarantee persistence after a process/system crash or a filesystem write failure.
The Mint tray icon provides Show/Hide Desktop Pet, Show Statistics, Pause/Resume,
Always on Top and Quit. **Hide Controls** leaves just Mint; restore controls with
**Show Pet Controls** in the tray menu or Mint's right-click menu. Hiding the Pet
keeps monitoring active. Quit removes the icon, stops GUI timers and exits the Qt
event loop; running monitor work finishes safely as described above.

The tray menu also has a read-only summary: Active/Paused/Unavailable status,
today's returned UsageTrim operation count and signed net token reduction.
These are existing local OpenAI token estimates, not AI-provider billing or
account balances. Negative net reduction remains negative and indicates expansion,
not savings. Empty history is shown as no measurements; initial loading and
monitor errors show unavailable values. The summary follows the same five-second
monitor updates even while Mint is hidden; opening the menu adds no data reads
or network requests.

Tray support depends on the desktop shell (including GNOME/Wayland). No GNOME
extension is required: without a detected tray, all controls remain available and
hiding the whole Pet is disabled. If tray support disappears, controls and a
previously hidden Pet are restored on the next availability check (every five
seconds). Mint's right-click menu provides another route to statistics and Quit.
Control visibility and hidden-window state are session-only; startup always keeps
controls accessible.

Mint gently floats by 2–3 pixels inside its widget, with a cached bitmap and at
most ten idle redraws per second. The window itself does not move. A small status
indicator and tooltip distinguish idle, paused, new measurements and unavailable
monitor data. Activity means an increase in today's returned UsageTrim operation
count between existing snapshots, not a running command or invented savings.
Existing history at startup, a day rollover or a cleared history does not trigger
activity. Missing history is idle; read errors are unavailable. Activity feedback
is brief, and a paused pet is muted and returns gently to its neutral position.

Turn off **Mint animations** in the panel or tray/right-click menu to keep Mint
still; the preference is saved in `companion.json`. Static state indicators remain
available. Motion stops when the Pet is hidden, minimized or replaced by the panel,
and resumes when shown. Quit stops all pet timers and animations. There is no
blinking or eye-layer manipulation of the original PNG, and no notifications or
account-limit network polling. Animation adds no monitor or provider requests.

CI runs the companion tests on Ubuntu and Windows (Python 3.12 on Windows) using
Qt's `offscreen` plugin and mocked system tray support. The Windows job installs
locked dependencies, loads the real Windows Qt bindings, queries native DWM and
checks the interpreter's final exit status, including failures after tests pass.
This verifies headless behavior, not the appearance of an interactive desktop.
The Qt 6.12.0 binding release is excluded because of the observed Python 3.11
shutdown crash; the lockfile currently selects Essentials/Shiboken 6.11.3.

Manual checks remain necessary on Windows, X11 with/without a compositor, and
actual Wayland sessions: transparency, native dragging, focus/always-on-top,
multi-monitor and mixed-DPI placement, tray visibility and disappearance, and Quit
from a hidden Pet. On Wayland, tests model rejected global positioning; they do
not establish support for every compositor's move protocol. Tray tests cannot
confirm whether a particular GNOME/KDE setup or Windows notification area exposes
the icon. Missing tray support keeps controls accessible as described above.

This command leaves the
existing macOS application unchanged; on macOS use the application below.

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

Possible follow-up work, separate from the features available today:

- Ranked repo map (Aider-style PageRank over the symbol graph) fitted to a token budget.
- LSP-backed references and rename (Serena-style), falling back to the ast-grep index.
- Lossless log templating (Drain-style) for dev-server, Docker and CI logs.
- A paired A/B harness on provider-reported tokens and task pass rate, because output-size savings alone can hide extra turns.

## Contributing

Lost context or an incorrect filter result? [Open an issue](https://github.com/00200200/usagetrim/issues/new)
with a small redacted input, the command, and the expected output.
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and checks.

[Guide](https://github.com/00200200/usagetrim/blob/main/docs/guide.md) · [Measurements](https://github.com/00200200/usagetrim/blob/main/docs/guide.md#measurements-and-development) · [MIT](https://github.com/00200200/usagetrim/blob/main/LICENSE)

<!-- mcp-name: io.github.00200200/usagetrim -->
