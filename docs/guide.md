# UsageTrim guide

[Back to the overview](../README.md)

UsageTrim reduces selected tool output and prepares bounded context. It does not
intercept every message, change subscription limits, or establish how long a
particular model's allowance will last. No additional model calls are required.

## Install and verify

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
(pipx works too).

```sh
uv tool install git+https://github.com/00200200/usagetrim
usagetrim demo
usagetrim doctor
```

To update, repeat the install command with `--force`. Reconnect existing MCP
sessions after upgrading. Configuration checks do not prove live-client use.

## Connect clients

### MCP profiles

`usagetrim mcp --profile desktop` is optimized for Claude Desktop and Codex Desktop: 11 essential tools with minified schemas, concise descriptions, traceback internal frame folding, and auto-skeleton large file folding.
`usagetrim mcp --profile coding` exposes code navigation, guarded edits, command
execution, targeted reads, recovery, diffs, task memory, statistics and local gain: 9 tools.
`usagetrim mcp --profile full` exposes all tools, including tree, JSON, clip,
pack, distill, table and optimize. The default remains `full` for existing configurations.
`USAGETRIM_MCP_PROFILE=desktop` or `coding` is equivalent; an explicit flag takes precedence.
Profiles affect discovery and dispatch, not CLI availability or command output.

### Claude Desktop & Codex Desktop

Use the automated installer to register the desktop profile:

```sh
usagetrim install --claude-desktop --profile desktop
usagetrim install --codex --profile desktop
# or configure both at once:
usagetrim install --all
```

For Claude Code CLI:

```sh
claude mcp add --scope user usagetrim -- usagetrim mcp --profile coding
```

### Codex

```sh
usagetrim install --codex --profile desktop
# or via CLI:
codex mcp add usagetrim -- usagetrim mcp --profile desktop
```

Update existing registrations and reconnect after changing profiles. Desktop
clients may need the absolute binary path returned by `command -v usagetrim`.

Run `usagetrim run -- <command> <args>` inside the native shell tool to retain
Codex's sandbox and approval flow. Keep short commands direct; do not rerun a
successful command merely to compress its output. Use a cache writable inside
the sandbox, and configure the same `USAGETRIM_CACHE_DIR` for MCP recovery.

UsageTrim does not install a Codex tool-output replacement hook. Its optional
task-memory hooks are separate. Local MCP configuration does not configure
ChatGPT web or establish access to ChatGPT chat quotas.

### Claude Desktop, Cursor, Windsurf and other MCP clients

Merge this server into the client's existing configuration:

```json
{
  "mcpServers": {
    "usagetrim": {
      "command": "/absolute/path/to/usagetrim",
      "args": ["mcp", "--profile", "coding"]
    }
  }
}
```

Use the client's MCP settings for Antigravity or Gemini CLI. UsageTrim also has
configuration helpers: `usagetrim install --claude-desktop`, `--cursor`, or
`--windsurf`. These currently register the backward-compatible full profile;
edit the server arguments to select `coding`. Avoid `--all` unless you want every
supported integration configured. Tools become available to the agent; this
does not automatically filter every conversation or unrelated tool result.

Keep agent instructions short, for example:

> Use UsageTrim for large tool results and targeted reads when available. Preserve
> errors and exit status; recover omitted details when needed. Keep normal
> permissions and avoid extra calls for already short output.

## Commands and recovery

### Prepare input for any chat

Open **Prepare for chat…** from the pet menu or the dashboard's compose button.
The native, resizable macOS window compares an editable original and a read-only
preview. Paste reads the clipboard once, on click. Copy preview replaces the
clipboard only on click; UsageTrim never pastes or sends to another application.
Editing the original, mode or target invalidates the previous preview.

The default **Preserve diagnostics** mode folds recognized progress and exact
repeated lines, keeps complete diagnostic tails, and leaves unfamiliar prose
unchanged. **Conversation summary · lossy** is opt-in and uses local heuristics;
review goals, constraints, negations and decisions against the original. It
cannot guarantee equivalent task quality. A draft that cannot become smaller
is kept after credential redaction, rather than expanded with summary scaffolding.

```sh
usagetrim prepare --file draft.txt
usagetrim prepare --file supplied-transcript.md --mode summary --budget 2000 --json
```

CLI input comes from the selected file or stdin, never an implicit clipboard
read. Both interfaces accept up to 128 KiB of UTF-8 text and make no AI calls.
Preview counts use `o200k_base` and print as `before → after (Δ · %)` on stderr
and in the pet window; they do not enter the savings ledger because UsageTrim
does not know whether you use the result. The default summary/optimize budget
is 2,000 tokens. Original drafts stay in app memory until cleared or quit;
compaction may store a redacted recovery copy in the existing cache. No draft
text is written to telemetry or statistics exports.

This works **before** sending input to Codex, Claude Desktop (including Chat),
Claude Code or other CLI clients. Existing chat history stays under the client's
control. The documented prompt hooks in [Codex](https://learn.chatgpt.com/docs/hooks)
and [Claude Code](https://code.claude.com/docs/en/hooks) add context or block
submission; they do not provide a transparent replacement of the full conversation.
Installing MCP does not change this. Task checkpoints below are a separate way
to carry selected information between native compactions.

### Tool output

```sh
usagetrim run -- pytest -v
usagetrim cat src/app.py --skeleton
usagetrim code "$PWD" --mode symbols --query Cache
usagetrim cat src/app.py --symbol Cache.get
usagetrim diff --staged
usagetrim retrieve tc_REFERENCE --lines 10-40
```

Default `run` uses conservative filtering and preserves unknown output and
diagnostics. `--engine auto` and `--engine usagetrim` use UsageTrim's own filter;
`--engine none` returns raw output. A command executes once. Explicit `--budget`
or `--compact` permits stronger, potentially lossy reduction. Follow recovery
references before relying on omitted information.

Identical `run` payloads and identical `cat` / MCP `usagetrim_read` views within
about 15 minutes collapse to a short cache ref (session dedup). Small reads stay
inline. Use `usagetrim retrieve` when the full text is needed again.

Recovered text is additional context, included as a cost in the net counter.
References recover the redacted cached original, not secrets removed before
storage. Secret-pattern matching is best effort, not a complete secret scanner.

### MCP CallToolResult compaction

Third-party MCP tool results are compacted as typed `CallToolResult` values, not
flattened strings. The PostToolUse hook uses the default **lossless** policy.

| Policy | What reaches the model | What is stored locally |
| --- | --- | --- |
| `lossless` (default) | Compacted `text` blocks; images, audio, resources, resource links, `structuredContent`, `isError`, annotations, `_meta`, and unknown extension keys unchanged | Nothing extra; optional session dedup only if other features already cache text |
| `reference` (opt-in) | Same text compaction, plus oversized opaque blocks replaced by a truthful type/size summary naming the withheld block and a `usagetrim retrieve` ref | Redacted JSON of each replaced block in the local SQLite cache (`USAGETRIM_CACHE_DIR`) |

`isError: true` results never use reference mode: error text, warnings, and opaque
attachments stay inline so safety-relevant content is never summarized away.
Lossless text rewrites may still shorten JSON tables inside error text when that
is strictly smaller and round-trippable.

Privacy and lifetime: recovery entries live in the local cache database until you
clear it (`usagetrim` cache clear / deleting the DB). They are not uploaded.
Recognized secret patterns are redacted before store; matching is best effort.
Session dedup notices for identical reads expire after about 15 minutes; spilled
MCP reference payloads persist until cleared. Per-result and per-block token
savings are reported by `compact_mcp_result` for callers that record metrics.

### Code navigation and editing

`usagetrim code` supports `map`, `symbols`, `occurrences`, `search`, `pattern`,
`outline`, `callers` and `references`. Use a specific file and query to keep
results focused. Name matches and call-site indexing use syntax; they are not
equivalent to compiler-backed reference resolution or semantic rename.

`usagetrim edit-symbol` previews a replacement. Supply `--replacement-file` and
the `--expected-hash` returned by a symbol read; add `--apply` only to write the
reviewed change. Stale hashes and invalid replacements fail before writing.
The MCP equivalent is `usagetrim_edit_symbol`. Follow the client's normal edit
permissions; do not grant blanket command approval to the wrapper.

### Explicit text preparation

| Command | Use |
| --- | --- |
| `usagetrim optimize [target] --budget 2000` | Autonomous optimizer: intra-fence compaction, TOON, distill, cache align. |
| `usagetrim prepare --file draft.txt [--mode optimize]` | Preview shorter input on device before pasting into chat. |
| `usagetrim clip --file error.log --budget 1500` | Prepare a compact log for pasting. |
| `usagetrim pack src tests --budget 4000 --skeleton` | Package selected files within a context budget. |
| `usagetrim distill --file conversation.md --budget 800` | Extract a heuristic summary of a supplied transcript. |
| `usagetrim table export.json --format markdown` | Format structured rows more compactly. |
| `usagetrim prompt lint CLAUDE.md` | Inspect a prompt template for potentially volatile content. |
| `usagetrim prompt align prompt.txt --output aligned.txt` | Prepare a reordered template for review. |

Use `--copy` only when you want a command to replace clipboard contents.
These transforms are explicit operations, not background chat interception.
Summaries, skeletons, table previews and prompt reordering can change or omit
meaning. Inspect the result and recover details where needed. Provider cache
behavior must be measured separately; a linter score is not a cache-hit rate.

Other commands: `tree` (repository token profile), `json` (JSON preview), `pipe`
(stdin filtering), `pr` (repository text delta), `stats` (text measurements),
`cache` (recovery cache management), `lint` (instruction file audit).
Run `usagetrim COMMAND --help` for current options.

## Task memory

`usagetrim_context` supports `save`, `read`, `list` and `forget`. Each task has an
absolute `root`, a distinct task ID, and a checkpoint containing `goal`,
`constraints`, `decisions`, `progress`, `next_steps` and `references`.

Saving requires `expected_revision`: `0` for a new task, otherwise the last read
revision. Conflicting updates fail. Notes have a 1,500-token cap with no silent
truncation, and the latest 20 revisions are retained. `forget` requires the
current revision and deletes the task's retained notes. CLI requests accept the
same JSON through `usagetrim context --request-file request.json` or stdin.

Opt into hooks after configuring MCP, using each client's actual cache path:

```sh
usagetrim context-install --client codex --cache-dir /absolute/path/to/codex-cache
usagetrim context-install --client claude-code --cache-dir "$HOME/.usagetrim"
```

The installer preserves settings with backups. **Codex requires native hook
review and trust**; UsageTrim does not bypass it. Reopen client sessions to load
changes. Configured hooks and observed hook execution are different states.

The agent saves short checkpoints at milestones during its existing work.
Session hooks prepare the same session's latest notes after compaction or
resume. Another task is not loaded just because it shares a project; `clear`
does not restore old notes. Moving to a different task requires explicitly
reading the earlier checkpoint. The client controls native compaction, which
may itself consume provider usage.

Task memory does not scan transcripts, prompts or native summaries. The
separate `distill` command reads only text explicitly supplied to it. Notes are
fallible data; newer user requests and current files take precedence.

## macOS companion

Requires macOS 13+ and Apple's Swift toolchain. From a source checkout:

```sh
git clone https://github.com/00200200/usagetrim.git
cd usagetrim
uv tool install --force .
bash macos/build.sh
open macos/build/UsageTrim.app
```

The local `.app` is ad-hoc signed, not notarized. The pet uses bundled 3D-rendered
artwork, supports dragging, remembers its position, and can be hidden with ×.
Monitoring continues in the menu bar. **Show pet** restores it. English UI,
system appearance, keyboard shortcuts and Reduce Motion support are built in.

The companion owns a `usagetrim monitor --stdio` subprocess: no cloud server,
listening port or extra AI call. It refreshes at most every five seconds while
visible and every thirty seconds in the background. Pause affects new wrapper,
MCP and hook transformations. Recovery remains available.

- **Savings:** before/after output, recovery cost, seven-day history and breakdowns.
- **Tools:** configuration and the last observed measurement; an isolated MCP check.
- **Limits:** available provider account readings, separately from text savings.
- **Context:** checkpoint metadata, observed native compaction and prepared overhead.

Codex quota reads use the local CLI account, which may differ from the app's
account. Claude reads optionally use the [CodexBar CLI](https://github.com/steipete/CodexBar)
adapter's terminal CLI source. A missing terminal login does not mean the Code
tab in Claude Desktop is signed out. ChatGPT chat and Antigravity quotas are not
connected. Unavailable data is not shown as a full allowance.

The pet labels the selected window (`5h` or `7d`) beside its remaining percentage.
When readings are missing, it shows an action such as **Connect**, **Set up** or
**Retry**. Click the provider for its specific cause. **Connect** for Claude means
the quota reader needs a connection, not that Claude Desktop is signed out.
In Terminal, run `claude auth login --claudeai`, complete the normal subscription
sign-in, then click **Refresh limits**. The panel can copy this command for you.
This does not change the active Claude Desktop session. No model call is required.
Allow up to one minute for the terminal reader's usage/status cycle, especially
after first sign-in. It runs in the background, so the panel remains responsive.

## Storage and measurements

`USAGETRIM_CACHE_DIR` selects client storage (default `~/.usagetrim`).
`cache.db` holds redacted recoverable content; `context.db` holds explicit task
notes. Clearing that directory also removes those notes. `telemetry.db` stores
counters and metadata, not command/output content. Recognized credential
patterns are redacted before caching; do not intentionally store secrets.

The companion discovers configured caches, including a sandbox-writable Codex
cache, without expanding sandbox permissions. Aggregates live in
`~/.usagetrim/metrics.db`; `USAGETRIM_STATE_DIR` isolates companion state. Statistics
exports exclude cached text and notes. Preference/configuration changes have
backups; restoring one reverses the corresponding change.

The main counter uses local `o200k_base` text estimates and includes recovery
costs; net results may be negative. Hook output is **prepared**, not confirmed
model delivery, and stays separate. Context restoration adds text and is
charged as overhead. Historical counts without attribution remain separate.

Full chat prompts, reasoning, provider caching, tool-loading policy, native
compaction cost and successful task completion are not all measured here.
Text reduction is not the same thing as an equal reduction in billed or
subscription usage. There is no verified SOTA or Serena/RTK performance claim.

## Measurements and development

```sh
uv sync --all-extras
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/benchmark_suite.py --json
```

The suite measures authored logs, a synthetic lockfile, source skeletons and
bounded MCP reads. It checks the complete safe-mode failure tail, exact recovery
of the cached original, and pass-through of unfamiliar output. Schema counts
cover serialized tool definitions, not provider billing. The full/coding
measurement is reproducible and updates when schemas change.

No models are called. These fixture checks are not an agent-quality benchmark;
comparison requires equal tasks, successful results, retries and recovery costs.
Do not publish fixture percentages as guaranteed account savings.

README artwork reuses `assets/pet-3d.png`. To rebuild its four-second animation
and reduced-motion poster, install FFmpeg, librsvg and ImageMagick and run
`bash scripts/render_readme.sh`. It illustrates the product; it is not a live
recording of quota readings. The original pet artwork is unchanged.

[Report a reproducible issue](https://github.com/00200200/usagetrim/issues/new) ·
[MIT license](../LICENSE)
