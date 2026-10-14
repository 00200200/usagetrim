# Benchmark fixture provenance

Five fixtures are captured outputs from real tool executions on small, controlled
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

All captures used a temporary directory outside the repository. Python used the
existing local interpreter; Node tools were installed in an isolated npm project
with exact package versions and npm 11.21.0. The command names above identify the
actual arguments; Python resolved to the local venv executable and Node tools to
the isolated project's `node_modules/.bin/`. No helper project or dependency was
added to this repository. Pytest used `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; captures
used `NO_COLOR=1`, with TypeScript's explicit `--pretty` retaining ANSI.

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

TypeScript's ANSI pretty output is an intentional captured no-op guard. ESLint's
small output expands by one token including the recovery notice; it is retained
without padding. Pyright keeps primary diagnostic headers and summary but drops
continuation details, including rule IDs. Its required signals check the primary
locations/messages and summary, not preservation of those omitted details.
