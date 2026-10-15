# Benchmark fixture provenance

Twenty-five fixtures are captured outputs from real tool executions on small, controlled
projects authored for this corpus. They are not production-project dumps. The
three original authored/smoke fixtures are not counted as captured outputs.

| case_id | classification | tool + version | command | acquisition | anonymization |
| --- | --- | --- | --- | --- | --- |
| pytest-failure | authored/smoke | Pytest-style output; no captured version | `pytest -v` | Hand-authored pass records and assertion diagnostic | Fictional paths and identifiers |
| cargo-success | authored/smoke | Cargo-style output; no captured version | `cargo test` | Hand-authored eight-test success output | Fictional module names |
| passthrough | authored/smoke | No actual tool | `custom-tool` | Hand-authored no-op output | None |
| pytest-verbose-success | captured | pytest 9.1.1; Python 3.12.7; pluggy 1.6.0 | `python -m pytest -v --color=no tests` | Eight parameterized currency-rounding checks and six status-normalization checks; exit 0 | Capture root replaced with `/workspace/capture`; interpreter replaced with `/opt/capture-venv/bin/python` |
| pytest-verbose-assertion | captured | pytest 9.1.1; Python 3.12.7; pluggy 1.6.0 | `python -m pytest -v --color=no tests` | Same billing suite plus an invoice-total assertion exposing omitted shipping; exit 1 | Same substitutions as pytest success |
| tsc-pretty-type-errors | captured | TypeScript 5.8.3; Node 22.17.1 | `tsc --noEmit --pretty` | Strict/noEmit project with three source files: missing email, optional token, incorrect numeric return; exit 2 | None; relative source paths and original ANSI retained |
| eslint-stylish-mixed-severity | captured | ESLint 9.24.0; Node 22.17.1 | `eslint src --format stylish` | Two JavaScript files; flat config enables no-unused-vars/error, no-console/warn, eqeqeq/error; exit 1 | Capture root replaced with `/workspace/capture` |
| pyright-source-diagnostics | captured | Pyright 1.1.398; Node 22.17.1 | `pyright` | Strict Python 3.12 project with three files: incompatible returns and optional token argument; exit 1 | Capture root replaced with `/workspace/capture` |
| pytest-xdist-captured-failure | captured | pytest 9.1.1; pytest-xdist 3.8.0; Python 3.12.7; pluggy 1.6.0 | `python -m pytest -p xdist.plugin -n 2 -v --color=no tests` | Six billing/status tests on two workers, including an omitted-shipping assertion and captured stdout/stderr; exit 1 | Capture root replaced with `/workspace/capture`; interpreter replaced with `/opt/capture-venv/bin/python` |
| vitest-verbose-success | captured | Vitest 3.1.1; Node 22.17.1 | `vitest run --reporter verbose --maxWorkers 1 --minWorkers 1` | Five invoice/status checks in a controlled JavaScript project; exit 0 | Capture root replaced with `/workspace/capture` |
| jest-console-failure | captured | Jest 29.7.0; Node 22.17.1 | `jest --runInBand` | Two passing status tests with console logs and one failed shipping-total test with console.error; exit 1 | None; tool emits relative paths |
| ruff-full-diagnostics | captured | Ruff 0.9.10; Python 3.12.7 | `ruff check --output-format full src` | Two Python files with undefined currency, unused import and unused assignment; exit 1 | None; tool emits relative paths |
| mypy-pretty-errors | captured | Mypy 1.15.0; Python 3.12.7 | `python -m mypy --pretty --show-error-codes src` | Three Python files with incompatible returns and optional token argument, captured with `COLUMNS=120`; exit 1 | None; tool emits relative paths |
| tsc-plain-type-errors | captured | TypeScript 5.8.3; Node 22.17.1 | `tsc --noEmit --pretty false` | Same strict three-file project as the 7A pretty capture, with plain diagnostic formatting; exit 2 | None; tool emits relative paths |
| npm-install-success | captured | npm 11.21.0; Node 22.17.1 | `npm install --save-exact --no-audit --no-fund --cache /workspace/capture/npm-cache picocolors@1.1.1` | Actual registry installation of picocolors 1.1.1 into an empty private project; exit 0 | No output substitutions needed; command's machine-specific cache path is documented with `/workspace/capture/npm-cache` |
| git-diff-lockfile-code | captured | Git 2.45.2 | `git diff --no-color HEAD -- src/billing.py package-lock.json` | Local Git repository; shipping calculation fix plus actual npm-generated picocolors lock update from 1.1.0 to 1.1.1; exit 0 | None; relative paths, public package hashes |
| git-log-patch-history | captured | Git 2.45.2 | `git log --no-color -p -2 -- src/billing.py package-lock.json` | Two real commits in the controlled repository, including code and generated lockfile patches; exit 0 | None; author identity and commit dates explicitly configured to fixed capture-only values before committing |
| ripgrep-context-matches | captured | ripgrep 15.2.0 | `rg -n -C 1 invoice_total src` | Actual search over billing and checkout Python modules, including context lines; exit 0 | None; relative paths |
| pip-install-success | captured | pip 24.2; Python 3.12.7 | `python -m pip install --disable-pip-version-check --no-input --no-cache-dir --index-url https://pypi.org/simple packaging==24.2` | Actual packaging 24.2 installation into a fresh isolated venv from the public PyPI index; exit 0 | None needed in output |
| python-threaded-json-traceback | captured | Python 3.12.7 | `python parse_invoice.py` | Invalid invoice JSON parsed through ThreadPoolExecutor; real standard-library traceback and JSONDecodeError; exit 1 | Capture root replaced with `/workspace/capture`; standard-library root replaced with `/opt/python/lib/python3.12` |
| directory-scan-dependencies | captured | GNU findutils 4.10.0 | `find src .venv/lib/python3.12/site-packages/packaging -type f` | Actual listing of application files and the installed packaging dependency, including its generated bytecode; exit 0 | None; relative paths |
| curl-local-http-error | captured | curl 8.9.1; Python HTTP server 3.12.7 | `curl --noproxy "*" --verbose --no-progress-meter http://127.0.0.1:8000/invoice` | Real HTTP request to a temporary loopback-only server returning 422 with an invoice-validation JSON body; curl exit 0 because --fail is not used | Ephemeral loopback port replaced with 8000 in address and connection-detail lines; CRLF preserved |
| git-status-mixed-worktree | captured | Git 2.45.2 | `git status --untracked-files=all` | Controlled repository with modified billing code, deleted legacy module, and untracked implementation/test/docs files; exit 0 | None; relative paths and explicitly configured capture-only identity |
| git-log-stat-history | captured | Git 2.45.2 | `git log --no-color --stat -2` | Two real commits covering the initial modules and a shipping fix with additions/deletion; actual stat blocks; exit 0 | None; fictional author and fixed commit dates configured before execution |
| ripgrep-json-context | captured | ripgrep 15.2.0 | `rg --json -C 1 invoice_total src` | Actual NDJSON search events, context and summary from three application modules; exit 0 | None; relative paths and captured timing fields retained |
| pip-resolver-conflict | captured | pip 24.2; Python 3.12.7 | `python -m pip install --dry-run --ignore-installed --disable-pip-version-check --no-input --no-cache-dir --index-url https://pypi.org/simple packaging==24.2 packaging==25.0` | Real resolver rejection of incompatible explicit version pins; dry-run prevents installation; exit 1 | None needed in output |
| pytest-collection-import-failure | captured | pytest 9.1.1; pluggy 1.7.0; Python 3.12.7 | `python -m pytest -v --color=no tests` | Real collection failure importing an unavailable invoice gateway module, before any tests execute; exit 2 | Capture root replaced with `/workspace/capture`; interpreter replaced with `/opt/capture-venv/bin/python`; standard-library root replaced with `/opt/python/lib/python3.12` |
| pip-inspect-package-json | captured | pip 24.2; Python 3.12.7 | `python -m pip inspect --path /workspace/capture/inspect-site` | Actual pip JSON report restricted to packaging 24.2 installed in an isolated target directory; exit 0 | Capture root replaced with `/workspace/capture`; environment platform_release/platform_version values replaced with `capture-kernel-release`/`capture-kernel-version` without reserializing JSON |

All captures used a temporary directory outside the repository. The 7A Python
captures used the existing local interpreter; Node tools used an isolated npm project
with exact package versions and npm 11.21.0. The command names above identify the
actual arguments; Python resolved to the local venv executable and Node tools to
the isolated project's `node_modules/.bin/`. No helper project or dependency was
added to this repository. Pytest used `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; captures
used `NO_COLOR=1`, with TypeScript's explicit `--pretty` retaining ANSI.

The 7B Python tools were installed with exact versions into a separate temporary
venv; Vitest, Jest and TypeScript used a separate temporary npm project. Pytest
xdist was loaded explicitly with `-p xdist.plugin` while plugin autoload stayed
disabled. Mypy used `COLUMNS=120` to capture unwrapped diagnostic headers and error
codes. The initial width-80 capture is retained in the temporary evidence: the
current Mypy filter drops wrapped continuation lines, including their codes.
Neither capture was manually reflowed. Worker order, durations and start times
remain actual tool output and can vary on a fresh execution.

The 7C batch uses six new functional families: Git, ripgrep search, pip installs,
Python tracebacks, HTTP responses, and directory scans. Both Git cases use one
temporary repository with a fictional capture-only author and fixed commit dates.
The small package-lock.json files were generated by npm 11.21.0 using exact
picocolors versions; no lockfile output was hand-written. The Python venv was
created with Python 3.12.7 and pip 24.2. No global packages were installed.
The HTTP server bound only to 127.0.0.1 and was shut down after capture; no cloud
account, cluster, or external HTTP endpoint was used for that fixture. Original
HTTP Date headers and mixed CRLF/LF output are retained, not normalized.

Docker CLI and daemon 29.1.3 were available during planning, but buildx was absent;
no images were pulled or built. Cargo/rustc, Go, Terraform/OpenTofu, kubectl, Helm,
and uv were unavailable, so these families were not simulated.

The final 7D batch adds six captures: mixed Git status, Git stat history, ripgrep
NDJSON, pip resolver failure, pytest collection failure, and structured package
inspection. All helper projects stayed outside the repository. Pytest 9.1.1 was
installed in an isolated Python 3.12.7 venv; inspection was restricted to a separate
target containing packaging 24.2. The inspect snapshot includes genuine public
distribution metadata (including package description and license classifiers),
not hand-authored JSON. Only machine-specific paths and kernel metadata values
were substituted; JSON formatting, NDJSON timing fields and whitespace remain
unaltered. No additional toolchain, Docker builder, cloud account or cluster was
used. This completes 25 captured cases plus the three authored/smoke cases.

Each log is the combined stdout/stderr byte stream (`stderr=STDOUT`), modified only
by the path substitutions listed above. Timings, padding, line endings, trailing
spaces, and final newlines remain captured values. No diagnostic lines or repeated
records were added. UsageTrim secret redaction leaves all frozen fixtures unchanged.

Initial measurements with the existing local `.openai` estimator:

| case_id | routing | raw tokens | output tokens | reduction % | signals | exact recovery |
| --- | --- | ---: | ---: | ---: | --- | --- |
| pytest-verbose-success | safe | 438 | 223 | 49.086758 | PASS | PASS |
| pytest-verbose-assertion | safe | 593 | 377 | 36.424958 | PASS | PASS |
| tsc-pretty-type-errors | specialized: filter_tsc | 502 | 502 | 0.000000 | PASS | PASS |
| eslint-stylish-mixed-severity | specialized: filter_eslint | 133 | 134 | -0.751880 | PASS | PASS |
| pyright-source-diagnostics | specialized: filter_pyright | 227 | 177 | 22.026432 | PASS | PASS |
| pytest-xdist-captured-failure | safe | 617 | 530 | 14.100486 | PASS | PASS |
| vitest-verbose-success | specialized: filter_jest_vitest | 159 | 122 | 23.270440 | PASS | PASS |
| jest-console-failure | specialized: filter_jest_vitest | 325 | 314 | 3.384615 | PASS | PASS |
| ruff-full-diagnostics | specialized: filter_ruff | 217 | 141 | 35.023041 | PASS | PASS |
| mypy-pretty-errors | specialized: filter_mypy | 136 | 131 | 3.676471 | PASS | PASS |
| tsc-plain-type-errors | specialized: filter_tsc | 108 | 114 | -5.555556 | PASS | PASS |
| npm-install-success | specialized: filter_npm_install | 10 | 37 | -270.000000 | PASS | PASS |
| git-diff-lockfile-code | specialized: filter_git_diff | 458 | 130 | 71.615721 | PASS | PASS |
| git-log-patch-history | specialized: filter_git_log | 1003 | 322 | 67.896311 | PASS | PASS |
| ripgrep-context-matches | specialized: filter_ripgrep_output | 162 | 103 | 36.419753 | PASS | PASS |
| pip-install-success | specialized: filter_pip_install | 63 | 53 | 15.873016 | PASS | PASS |
| python-threaded-json-traceback | specialized: filter_traceback | 346 | 345 | 0.289017 | PASS | PASS |
| directory-scan-dependencies | specialized: filter_directory_scan | 763 | 59 | 92.267366 | PASS | PASS |
| curl-local-http-error | specialized: filter_curl_http | 199 | 174 | 12.562814 | PASS | PASS |
| git-status-mixed-worktree | specialized: filter_git_status | 129 | 139 | -7.751938 | PASS | PASS |
| git-log-stat-history | specialized: filter_git_log | 251 | 179 | 28.685259 | PASS | PASS |
| ripgrep-json-context | specialized: filter_ripgrep_output | 900 | 115 | 87.222222 | PASS | PASS |
| pip-resolver-conflict | specialized: filter_pip_install | 145 | 152 | -4.827586 | PASS | PASS |
| pytest-collection-import-failure | safe | 251 | 251 | 0.000000 | PASS | PASS |
| pip-inspect-package-json | specialized: filter_json_output | 1009 | 457 | 54.707631 | PASS | PASS |

TypeScript's ANSI pretty output is an intentional captured no-op guard. ESLint's
small output expands by one token including the recovery notice; it is retained
without padding. Pyright keeps primary diagnostic headers and summary but drops
continuation details, including rule IDs. Its required signals check the primary
locations/messages and summary, not preservation of those omitted details.

Plain TypeScript exercises recognized diagnostic headers but expands by six
tokens including recovery metadata. npm's short install summary expands by 27
tokens: the production filter changes whitespace, triggering the harness notice.
The percentage is large because the input contains only ten tokens. Both are
retained without padding. Required xdist signals check the assertion, location,
summary and captured-section headers; they do not require preservation of the
captured I/O bodies that safe filtering intentionally folds.

Git status and pip resolver failure grow by ten and seven tokens respectively
including recovery metadata; no output was padded to offset that overhead. The
pytest collection failure is a no-op at 251 tokens, below the safe filter's
256-token cutoff. Its specific missing-module diagnostics and collection status
remain useful preservation guards despite zero reduction.
