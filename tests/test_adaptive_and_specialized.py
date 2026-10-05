import json
import re

import pytest

from usagetrim.core.adaptive import compress_to_budget
from usagetrim.core.cache import ContextCache
from usagetrim.core.specialized import (
    author_helm_install_fixture,
    author_kubectl_describe_fixture,
    author_kubectl_get_fixture,
    author_kubectl_logs_fixture,
    author_pg_explain_analyze_fixture,
    author_terraform_plan_fixture,
    auto_specialize_command_output,
    filter_cargo_build,
    filter_cargo_test,
    filter_ci_logs,
    filter_docker_build,
    filter_eslint,
    filter_explain_plan,
    filter_git_diff,
    filter_git_log,
    filter_git_status,
    filter_go_test,
    filter_helm,
    filter_jest_vitest,
    filter_json_output,
    filter_kubectl,
    filter_mypy,
    filter_npm_install,
    filter_pip_install,
    filter_pyright,
    filter_ripgrep_output,
    filter_ruff,
    filter_sql_logs,
    filter_terraform,
    filter_terraform_plan,
    filter_tsc,
    filter_uv_project,
)
from usagetrim.metrics.tokenizer import count_tokens

SAMPLE_KUBECTL_DESCRIBE = author_kubectl_describe_fixture()
SAMPLE_KUBECTL_GET = author_kubectl_get_fixture()
SAMPLE_KUBECTL_LOGS = author_kubectl_logs_fixture()
SAMPLE_HELM_INSTALL = author_helm_install_fixture()
SAMPLE_TERRAFORM_PLAN = author_terraform_plan_fixture()

SAMPLE_GIT_LOG = """commit a1b2c3d4e5f67890abcdef1234567890abcdef12
Author: Alice Developer <alice@example.com>
Date:   Mon Sep 15 14:00:00 2026 +0200

    feat(auth): implement JWT verification and token refresh

commit f1e2d3c4b5a67890abcdef1234567890abcdef12
Author: Bob Maintainer <bob@example.com>
Date:   Sun Sep 14 10:00:00 2026 +0200

    fix(db): handle connection pool timeout gracefully
"""

SAMPLE_GIT_STATUS = """On branch main
Changes to be committed:
	modified:   src/main.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	tmp/cache/chunk1.json
	tmp/cache/chunk2.json
	tmp/cache/chunk3.json
	tmp/cache/chunk4.json
	single_file.txt
"""


SAMPLE_GIT_LOG_PATCH = """commit a1b2c3d4e5f67890abcdef1234567890abcdef12
Author: Alice Developer <alice@example.com>
Date:   Mon Sep 15 14:00:00 2026 +0200

    fix(api): guard against empty payloads

diff --git a/src/api.py b/src/api.py
index 1111111..2222222 100644
--- a/src/api.py
+++ b/src/api.py
@@ -10,6 +10,8 @@ class Handler:
     def handle(self, payload):
         logger.debug("incoming payload")
+        if not payload:
+            raise ValueError("empty payload")
         return self.process(payload)
"""

SAMPLE_GIT_LOG_STAT = """commit a1b2c3d4e5f67890abcdef1234567890abcdef12
Author: Alice Developer <alice@example.com>
Date:   Mon Sep 15 14:00:00 2026 +0200

    chore(deps): bump pinned versions

 README.md |  4 +-
 uv.lock   | 20 ++++++----
 2 files changed, 16 insertions(+), 8 deletions(-)
"""

SAMPLE_CARGO_PASS = "\n".join(
    [
        "   Compiling acme-core v0.4.1 (/src/acme-core)",
        "     Running unittests src/lib.rs (target/debug/deps/acme_core-3f9a2b1c)",
        "",
        "running 48 tests",
    ]
    + [f"test module{i // 8}::tests::case_{i} ... ok" for i in range(48)]
    + [
        "",
        "test result: ok. 48 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 0.31s",
    ]
)

SAMPLE_CARGO_FAIL = """running 6 tests
test config::tests::parses_defaults ... ok
test config::tests::rejects_unknown_keys ... ok
test parser::tests::handles_empty ... ok
test parser::tests::rejects_bad_utf8 ... FAILED
test parser::tests::roundtrip ... ok

failures:

---- parser::tests::rejects_bad_utf8 stdout ----
thread 'parser::tests::rejects_bad_utf8' panicked at src/parser.rs:212:9:
assertion `left == right` failed
  left: Err(InvalidUtf8)
 right: Ok(())

test result: FAILED. 5 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; \
finished in 0.08s
"""

SAMPLE_GO_PASS = "\n".join(
    [
        "=== RUN   TestServerStart",
        "--- PASS: TestServerStart (0.01s)",
        "=== RUN   TestRouteMatch",
        "--- PASS: TestRouteMatch (0.00s)",
        "=== RUN   TestMiddlewareAuth",
        "--- PASS: TestMiddlewareAuth (0.00s)",
        "PASS",
        "ok  \tgithub.com/acme/server\t0.034s",
    ]
)

SAMPLE_GO_FAIL = """=== RUN   TestDatabaseQuery
--- PASS: TestDatabaseQuery (0.01s)
=== RUN   TestPanicRecovery
--- FAIL: TestPanicRecovery (0.00s)
panic: unhandled nil pointer dereference [recovered]
	panic: runtime error: invalid memory address
goroutine 16 [running]:
main.TestPanicRecovery(0x1400011e1e0)
	/src/server_test.go:42 +0x28
FAIL
FAIL	github.com/acme/server	0.018s
FAIL
"""


SAMPLE_CARGO_LARGE = "\n".join(
    [
        "   Compiling acme-core v0.4.1 (/src/acme-core)",
        "     Running unittests src/lib.rs (target/debug/deps/acme_core-3f9a2b1c)",
        "",
        "running 201 tests",
    ]
    + [f"test module{i // 10}::tests::case_{i} ... ok" for i in range(200)]
    + [
        "test parser::tests::rejects_bad_utf8 ... FAILED",
        "",
        "failures:",
        "",
        "---- parser::tests::rejects_bad_utf8 stdout ----",
        "thread 'parser::tests::rejects_bad_utf8' panicked at src/parser.rs:212:9:",
        "assertion `left == right` failed",
        "  left: Err(InvalidUtf8)",
        " right: Ok(())",
        "",
        "stack backtrace:",
    ]
    + [f"   {i}: acme_core::parser::parse_{i}" for i in range(48)]
    + [f"             at ./src/parser.rs:{200 + i}:5" for i in range(48)]
    + [
        "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace",
        "",
        "failures:",
        "    parser::tests::rejects_bad_utf8",
        "",
        "test result: FAILED. 200 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 1.24s",
    ]
)

SAMPLE_GO_LARGE = "\n".join(
    sum(
        ([f"=== RUN   TestOk{i}", f"--- PASS: TestOk{i} (0.00s)"] for i in range(120)),
        [],
    )
    + [
        "=== RUN   TestPanicRecovery",
        "--- FAIL: TestPanicRecovery (0.00s)",
        "panic: unhandled nil pointer dereference [recovered]",
        "\tpanic: runtime error: invalid memory address",
        "goroutine 16 [running]:",
    ]
    + [f"main.helper{i}(0x1400011e1e0)" for i in range(36)]
    + [f"\t/src/server_test.go:{40 + i} +0x28" for i in range(36)]
    + [
        "FAIL",
        "FAIL\tgithub.com/acme/server\t0.018s",
        "FAIL",
    ]
)

SAMPLE_NEXTEST_PASS = "\n".join(
    [
        "    Starting 24 tests across 1 binary",
    ]
    + [f"        PASS [   0.01{i % 10}s] acme tests::case_{i}" for i in range(24)]
    + [
        "     Summary [   0.42s] 24 tests run: 24 passed, 0 skipped",
    ]
)

SAMPLE_VITEST_PASS = """ ✓ src/utils/format.test.ts (4 tests) 12ms
   ✓ formats currency correctly (2ms)
   ✓ parses date string (1ms)
   ✓ handles empty input (1ms)
   ✓ escapes html entities (1ms)
 ✓ src/components/Button.test.tsx (3 tests) 20ms
   ✓ renders button label (3ms)
   ✓ triggers click handler (2ms)
   ✓ respects disabled state (2ms)

 Test Files  2 passed (2)
      Tests  7 passed (7)
   Start at  08:00:00
   Duration  45ms
"""

SAMPLE_JEST_FAIL = """PASS src/utils/math.test.js
  ✓ calculates sum (2 ms)

FAIL src/utils/parser.test.js
  ● Parser › rejects malformed input

    expect(received).toThrow()

    Expected substring: "invalid token"
    Received message:   "unexpected EOF"

      18 |   test('rejects malformed input', () => {
    > 19 |     expect(() => parse(raw)).toThrow('invalid token');
         |                              ^
      20 |   });

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 1 passed, 2 total
Snapshots:   0 total
Time:        0.85 s
"""

SAMPLE_TSC_ERRORS = """
src/auth/jwt.ts:45:12 - error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.
  Type 'undefined' is not assignable to type 'string'.

  43 |   const headerToken = headers.get("authorization") ?? undefined;
  44 |   // verify the bearer token before continuing the chain
> 45 |   verifyToken(headerToken);
     |               ~~~~~~~~~~~
  46 |   return next();
  47 | }

src/auth/jwt.ts:45:12 - error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.
  Type 'undefined' is not assignable to type 'string'.

  43 |   const headerToken = headers.get("authorization") ?? undefined;
  44 |   // verify the bearer token before continuing the chain
> 45 |   verifyToken(headerToken);
     |               ~~~~~~~~~~~
  46 |   return next();
  47 | }

src/models/user.ts:18:7 - error TS2741: Property 'email' is missing in type '{ id: number; name: string; }' but required in type 'User'.

  16 | export function buildUser(id: number, name: string): User {
  17 |   // TODO: pull email from the directory service
> 18 |   const u: User = { id: 1, name: "Alice" };
     |         ~
  19 |   return u;
  20 | }

src/models/user.ts:18:7 - error TS2741: Property 'email' is missing in type '{ id: number; name: string; }' but required in type 'User'.

  16 | export function buildUser(id: number, name: string): User {
  17 |   // TODO: pull email from the directory service
> 18 |   const u: User = { id: 1, name: "Alice" };
     |         ~
  19 |   return u;
  20 | }

src/api/billing.ts:88:3 - error TS2322: Type 'Promise<Payment | null>' is not assignable to type 'Promise<Payment>'.
  Type 'Payment | null' is not assignable to type 'Payment'.
    Type 'null' is not assignable to type 'Payment'.

  86 | async function charge(orderId: string): Promise<Payment> {
  87 |   const client = await getClient();
> 88 |   return client.charge(orderId);
     |   ~~~~~~
  89 | }
  90 |

src/api/billing.ts:88:3 - error TS2322: Type 'Promise<Payment | null>' is not assignable to type 'Promise<Payment>'.
  Type 'Payment | null' is not assignable to type 'Payment'.
    Type 'null' is not assignable to type 'Payment'.

  86 | async function charge(orderId: string): Promise<Payment> {
  87 |   const client = await getClient();
> 88 |   return client.charge(orderId);
     |   ~~~~~~
  89 | }
  90 |

Found 6 errors in 3 files.

Errors  Files
     2  src/auth/jwt.ts:45
     2  src/models/user.ts:18
     2  src/api/billing.ts:88
""".lstrip()


SAMPLE_GIT_DIFF = """diff --git a/src/main.py b/src/main.py
index 1234567..89abcdef 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,6 +10,7 @@ def process():
     context_line_1
     context_line_2
     context_line_3
+    new_important_logic()
     context_line_4
     context_line_5
diff --git a/uv.lock b/uv.lock
index aaaaaaa..bbbbbbb 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,500 +1,500 @@
-old_package_version = "1.0.0"
+new_package_version = "1.0.1"
""" + "\n".join([f"+ extra_lock_line_{i}" for i in range(100)])

SAMPLE_RUFF_FULL = """src/auth/session.py:12:8: F401 [*] `os` imported but unused
  |
10 | import sys
11 | import json
12 | import os
   |        ^^
  |
  = help: Remove unused import: `os`

src/auth/session.py:44:5: F841 Local variable `token` is assigned to but never used
  |
42 | def refresh():
43 |     client = Client()
44 |     token = client.issue()
   |     ^^^^^
45 |     return client
  |
  = help: Remove assignment to unused variable `token`

Found 2 errors.
[*] 1 fixable with the `--fix` option.
"""

# ESLint stylish repeats absolute path headers; duplicates simulate noisy CI dumps.
SAMPLE_ESLINT_STYLISH = """
/Users/dev/acme/src/auth/session.ts
  12:8   error    'os' is defined but never used                 @typescript-eslint/no-unused-vars
  44:5   warning  Unexpected any. Specify a different type       @typescript-eslint/no-explicit-any

/Users/dev/acme/src/auth/session.ts
  12:8   error    'os' is defined but never used                 @typescript-eslint/no-unused-vars
  44:5   warning  Unexpected any. Specify a different type       @typescript-eslint/no-explicit-any

/Users/dev/acme/src/utils/helpers.ts
   3:1   error    Prefer named exports                           import/prefer-default-export
  18:10  error    'token' is assigned a value but never used     @typescript-eslint/no-unused-vars

/Users/dev/acme/src/utils/helpers.ts
   3:1   error    Prefer named exports                           import/prefer-default-export
  18:10  error    'token' is assigned a value but never used     @typescript-eslint/no-unused-vars

/Users/dev/acme/src/api/billing.ts
  22:14  error    'req' is defined but never used                @typescript-eslint/no-unused-vars
  55:3   error    Expected '!==' and instead saw '!='            eqeqeq
  71:9   warning  Unexpected console statement                   no-console

✖ 11 problems (8 errors, 3 warnings)
  2 errors and 0 warnings potentially fixable with the `--fix` option.
""".lstrip()

# Codeframe embeds source, carets, and stack noise per diagnostic.
SAMPLE_ESLINT_CODEFRAME = """
error: 'os' is defined but never used (@typescript-eslint/no-unused-vars) at src/auth/session.ts:12:8:
  10 | import sys from "sys";
  11 | import json from "json";
> 12 | import os from "os";
     |        ^
  13 |
  at Object.<anonymous> (src/auth/session.ts:12:8)
  at Module._compile (node:internal/modules/cjs/loader:1521:14)

error: 'os' is defined but never used (@typescript-eslint/no-unused-vars) at src/auth/session.ts:12:8:
  10 | import sys from "sys";
  11 | import json from "json";
> 12 | import os from "os";
     |        ^
  13 |
  at Object.<anonymous> (src/auth/session.ts:12:8)
  at Module._compile (node:internal/modules/cjs/loader:1521:14)

error: Unexpected any. Specify a different type (@typescript-eslint/no-explicit-any) at src/auth/session.ts:44:5:
  42 | function refresh() {
  43 |   const client = new Client();
> 44 |   const token: any = client.issue();
     |     ^^^
  45 |   return client;
  46 | }
  at refresh (src/auth/session.ts:44:5)
  at processTicksAndRejections (node:internal/process/task_queues:95:5)

error: Prefer named exports (import/prefer-default-export) at src/utils/helpers.ts:3:1:
   1 | import lodash from "lodash";
   2 |
>  3 | export default function helpers() {
     | ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   4 |   return lodash;
   5 | }

error: 'token' is assigned a value but never used (@typescript-eslint/no-unused-vars) at src/utils/helpers.ts:18:10:
  16 | export function mint() {
  17 |   const client = new Client();
> 18 |   const token = client.issue();
     |          ^^^^^
  19 |   return client;
  20 | }

✖ 5 problems (5 errors, 0 warnings)
""".lstrip()


def _sample_docker_build_fail() -> str:
    """Authored BuildKit-style log: lots of layer progress, failure preserved at the end."""
    lines: list[str] = []
    for step in range(1, 25):
        lines.extend(
            [
                f"#{step} [internal] load build context",
                f"#{step} transferring context: {120 + step}B done",
                f"#{step} DONE 0.{step % 9}s",
                (
                    f"#{step} [stage-0 {step}/24] RUN echo layer_{step} "
                    f"&& pip install pkg{step}==1.0.{step}"
                ),
            ]
        )
        for j in range(8):
            lines.extend(
                [
                    f"#{step} {j}.1 Collecting pkg{step}-dep{j}==2.0.{j}",
                    (
                        f"#{step} {j}.2   Downloading "
                        f"pkg{step}_dep{j}-2.0.{j}-py3-none-any.whl ({40 + j} kB)"
                    ),
                    f"#{step} {j}.3 Installing collected packages: pkg{step}-dep{j}",
                    f"#{step} {j}.4 Successfully installed pkg{step}-dep{j}-2.0.{j}",
                ]
            )
        lines.append(f"#{step} DONE {1 + step % 5}.{step % 9}s")
    lines.extend(
        [
            (
                'ERROR: failed to solve: process "/bin/sh -c pip install broken==9.9.9" '
                "did not complete successfully: exit code: 1"
            ),
            "------",
            " > [stage-0 24/24] RUN pip install broken==9.9.9:",
            "1.2 ERROR: Could not find a version that satisfies the requirement broken==9.9.9",
            "1.2 ERROR: No matching distribution found for broken==9.9.9",
            "------",
        ]
    )
    return "\n".join(lines)


SAMPLE_MYPY_PRETTY = """src/auth/session.py:12: error: Name "os" is not defined  [name-defined]
    |
  10 | import sys
  11 | import json
  12 | print(os.getcwd())
    |           ^
  13 | return True
    |
src/auth/session.py:44: error: Incompatible return value type (got "None", expected "str")  [return-value]
    |
  42 | def refresh() -> str:
  43 |     client = Client()
  44 |     return None
    |            ^
    |
src/models/user.py:18: error: Missing named argument "email" for "User"  [call-arg]
    |
  17 | def build():
  18 |     return User(id=1, name="Alice")
    |            ^
    |
src/models/user.py:31: error: Incompatible types in assignment (expression has type "str", variable has type "int")  [assignment]
    |
  30 | age: int
  31 | age = "thirty"
    |       ^
  32 | return age
    |
src/api/handlers.py:7: error: Argument 1 to "loads" has incompatible type "bytes"; expected "str"  [arg-type]
    |
   5 | import json
   6 | def parse(raw: bytes):
   7 |     return json.loads(raw)
    |                         ^
    |
src/api/handlers.py:22: error: Item "None" of "str | None" has no attribute "strip"  [union-attr]
    |
  21 | def clean(value: str | None) -> str:
  22 |     return value.strip()
    |            ^
    |
src/db/pool.py:55: error: Need type annotation for "cache"  [var-annotated]
    |
  53 | class Pool:
  54 |     def __init__(self):
  55 |         self.cache = {}
    |              ^
    |
src/db/pool.py:88: error: Returning Any from function declared to return "Connection"  [no-any-return]
    |
  87 | def connect(self):
  88 |     return self._factory()
    |            ^
    |
src/auth/session.py:44: note: Error code "return-value" not covered by "type: ignore" comment
Found 8 errors in 4 files (checked 24 source files)
"""

SAMPLE_DOCKER_BUILD_FAIL = _sample_docker_build_fail()

SAMPLE_DOCKER_BUILD_OK = "\n".join(
    [f"#{i} [stage-0 {i}/6] RUN echo ok_{i}" for i in range(1, 7)]
    + [
        "#6 DONE 0.2s",
        "#7 exporting to image",
        "#7 writing image sha256:abcdef0123456789",
        "#7 naming to docker.io/library/app:latest done",
        "#7 DONE 0.1s",
        "Successfully tagged app:latest",
    ]
)

# Pyright prints dense headers, then indented path + source + caret frames.
SAMPLE_PYRIGHT = "\n".join(
    [
        line
        for i in range(1, 30)
        for line in (
            (
                f"/Users/me/proj/src/mod_{i}.py:{12 + i}:5 - error: Type "
                f'"str | None" is not assignable to declared type "str" '
                f"(reportGeneralTypeIssues)"
            ),
            f"    /Users/me/proj/src/mod_{i}.py:{12 + i}:5",
            f"        {12 + i}     token_{i}: str = maybe_token_{i}",
            "               ~~~~~",
            (
                f"/Users/me/proj/src/mod_{i}.py:{28 + i}:16 - error: Argument of type "
                f'"int" cannot be assigned to parameter "user_id" of type "str" '
                f"(reportArgumentType)"
            ),
            f"    /Users/me/proj/src/mod_{i}.py:{28 + i}:16",
            f"        {28 + i}     lookup_user_{i}({i})",
            "                       ~~",
        )
    ]
    + ["58 errors, 0 warnings, 0 informations"]
)


def test_filter_git_log():
    compact = filter_git_log(SAMPLE_GIT_LOG)
    assert "a1b2c3d" in compact
    assert "[Alice Developer]" in compact
    assert "feat(auth): implement JWT" in compact
    assert "Date:" not in compact
    assert len(compact.splitlines()) < len(SAMPLE_GIT_LOG.splitlines())


def test_filter_git_status():
    compact = filter_git_status(SAMPLE_GIT_STATUS)
    assert "tmp/ (4 untracked files)" in compact
    assert "single_file.txt" in compact


def test_filter_git_log_keeps_patch_body():
    compact = filter_git_log(SAMPLE_GIT_LOG_PATCH)

    # The patch is the point of `git log -p`; it must survive compaction.
    assert "diff --git a/src/api.py b/src/api.py" in compact
    assert 'raise ValueError("empty payload")' in compact
    assert "Date:" not in compact


def test_filter_git_log_does_not_absorb_diff_context_into_message():
    compact = filter_git_log(SAMPLE_GIT_LOG_PATCH)
    subject = compact.splitlines()[0]

    # Context lines are indented like message lines, but are not message text.
    assert subject == "a1b2c3d [Alice Developer] fix(api): guard against empty payloads"
    assert "def handle" not in subject
    assert "logger.debug" not in subject


def test_filter_git_log_keeps_stat_block():
    compact = filter_git_log(SAMPLE_GIT_LOG_STAT)

    assert "a1b2c3d [Alice Developer] chore(deps): bump pinned versions" in compact
    assert "README.md" in compact
    assert "2 files changed, 16 insertions(+), 8 deletions(-)" in compact


def test_filter_git_log_plain_format_stays_dense():
    # The headline one-line-per-commit behaviour must not regress.
    compact = filter_git_log(SAMPLE_GIT_LOG)

    assert len(compact.splitlines()) == 2
    assert "diff --git" not in compact


def test_auto_specialize():
    res_log = auto_specialize_command_output("git log -n 10", SAMPLE_GIT_LOG)
    assert res_log is not None
    assert "a1b2c3d" in res_log

    res_none = auto_specialize_command_output("pytest -v", "some output")
    assert res_none is None


def test_filter_cargo_test_collapses_passing_runs():
    compact = filter_cargo_test(SAMPLE_CARGO_PASS)

    assert "[UsageTrim: 48 passing tests, 48 progress records]" in compact
    assert "test module0::tests::case_0 ... ok" not in compact
    # The authoritative summary and the compile banner must survive.
    assert "test result: ok. 48 passed; 0 failed" in compact
    assert "Compiling acme-core v0.4.1" in compact
    assert count_tokens(compact).claude < count_tokens(SAMPLE_CARGO_PASS).claude


def test_filter_cargo_test_keeps_failure_identity_and_assertion():
    compact = filter_cargo_test(SAMPLE_CARGO_FAIL)

    assert "test parser::tests::rejects_bad_utf8 ... FAILED" in compact
    assert "panicked at src/parser.rs:212:9" in compact
    assert "assertion `left == right` failed" in compact
    assert "left: Err(InvalidUtf8)" in compact
    assert "test result: FAILED. 5 passed; 1 failed" in compact
    # Passes after the failure stay visible; progress before it may collapse.
    assert "test parser::tests::roundtrip ... ok" in compact


def test_filter_cargo_test_leaves_output_without_passes_unchanged():
    raw = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored\n"

    assert filter_cargo_test(raw) == raw


def test_auto_specialize_routes_cargo_test():
    compact = auto_specialize_command_output("cargo test --all-features", SAMPLE_CARGO_PASS)

    assert compact is not None
    assert "[UsageTrim: 48 passing tests, 48 progress records]" in compact


def test_filter_cargo_test_collapses_tests_named_like_diagnostics():
    raw = "\n".join(
        ["running 3 tests"]
        + [
            "test error::tests::reports_failure ... ok",
            "test warning::tests::timeout_is_logged ... ok",
            "test parser::tests::panicked_input ... ok",
        ]
        + ["", "test result: ok. 3 passed; 0 failed; 0 ignored"]
    )

    compact = filter_cargo_test(raw)

    # "error"/"warning" inside a test name must not stop collapsing.
    assert "[UsageTrim: 3 passing tests, 3 progress records]" in compact
    assert "test result: ok. 3 passed; 0 failed" in compact


def test_filter_go_test_collapses_passing_runs():
    compact = filter_go_test(SAMPLE_GO_PASS)

    assert "[UsageTrim: 3 passing tests, 6 progress records]" in compact
    assert "=== RUN   TestServerStart" not in compact
    assert "--- PASS: TestServerStart" not in compact
    assert "ok  \tgithub.com/acme/server\t0.034s" in compact
    assert count_tokens(compact).claude < count_tokens(SAMPLE_GO_PASS).claude


def test_filter_go_test_keeps_failure_identity_and_assertion():
    compact = filter_go_test(SAMPLE_GO_FAIL)

    assert "--- FAIL: TestPanicRecovery" in compact
    assert "panic: unhandled nil pointer dereference" in compact
    assert "runtime error: invalid memory address" in compact
    assert "FAIL\tgithub.com/acme/server" in compact
    # Stack frames are noise once the panic line is kept.
    assert "main.TestPanicRecovery" not in compact


def test_auto_specialize_routes_go_test():
    compact = auto_specialize_command_output("go test -v ./...", SAMPLE_GO_PASS)
    assert compact is not None
    assert "[UsageTrim: 3 passing tests, 6 progress records]" in compact


def test_filter_cargo_test_compacts_failures_hard_with_large_savings():
    compact = filter_cargo_test(SAMPLE_CARGO_LARGE)
    raw_tokens = count_tokens(SAMPLE_CARGO_LARGE).openai
    out_tokens = count_tokens(compact).openai
    reduction = 100 * (raw_tokens - out_tokens) / raw_tokens

    # Pass/fail identity, names, assertion — not the 96-line backtrace.
    assert "test parser::tests::rejects_bad_utf8 ... FAILED" in compact
    assert "assertion `left == right` failed" in compact
    assert "left: Err(InvalidUtf8)" in compact
    assert "test result: FAILED. 200 passed; 1 failed" in compact
    assert "[UsageTrim: 200 passing tests, 200 progress records]" in compact
    assert "stack backtrace:" not in compact
    assert "acme_core::parser::parse_12" not in compact
    assert reduction >= 85.0, (
        f"expected >=85% savings, got {reduction:.1f}% ({raw_tokens}->{out_tokens})"
    )


def test_filter_go_test_compacts_failures_hard_with_large_savings():
    compact = filter_go_test(SAMPLE_GO_LARGE)
    raw_tokens = count_tokens(SAMPLE_GO_LARGE).openai
    out_tokens = count_tokens(compact).openai
    reduction = 100 * (raw_tokens - out_tokens) / raw_tokens

    assert "--- FAIL: TestPanicRecovery" in compact
    assert "panic: unhandled nil pointer dereference" in compact
    assert "runtime error: invalid memory address" in compact
    assert "FAIL\tgithub.com/acme/server" in compact
    assert "[UsageTrim: 120 passing tests, 241 progress records]" in compact
    assert "goroutine 16 [running]:" not in compact
    assert "main.helper12" not in compact
    assert reduction >= 85.0, (
        f"expected >=85% savings, got {reduction:.1f}% ({raw_tokens}->{out_tokens})"
    )


def test_auto_specialize_routes_cargo_toolchain_absolute_and_nextest():
    assert auto_specialize_command_output("cargo +nightly test", SAMPLE_CARGO_PASS) is not None
    assert (
        auto_specialize_command_output("cargo --locked test --all-features", SAMPLE_CARGO_PASS)
        is not None
    )
    assert (
        auto_specialize_command_output("/Users/me/.cargo/bin/cargo test", SAMPLE_CARGO_PASS)
        is not None
    )
    compact = auto_specialize_command_output("cargo nextest run", SAMPLE_NEXTEST_PASS)
    assert compact is not None
    assert "[UsageTrim: 24 passing tests, 24 progress records]" in compact
    assert "PASS [   0.010s]" not in compact


def test_auto_specialize_routes_absolute_go_test():
    compact = auto_specialize_command_output("/usr/local/go/bin/go test -v ./...", SAMPLE_GO_PASS)
    assert compact is not None
    assert "[UsageTrim: 3 passing tests, 6 progress records]" in compact


def test_filter_jest_vitest_collapses_passing_runs():
    compact = filter_jest_vitest(SAMPLE_VITEST_PASS)

    # Vitest's own summary in this fixture reports 7 tests across 2 files.
    assert "[UsageTrim: 7 passing tests in 2 files, 9 progress records]" in compact
    assert "formats currency correctly" not in compact
    assert "Test Files  2 passed (2)" in compact
    assert "Tests  7 passed (7)" in compact
    assert count_tokens(compact).claude < count_tokens(SAMPLE_VITEST_PASS).claude


def test_filter_jest_vitest_collapses_console_noise_keeps_failure_console():
    """Passing-suite console.log / vitest stdout dumps fold; failure tails stay."""
    chunks: list[str] = ["PASS src/widget.test.js", "  ✓ renders (2 ms)"]
    for i in range(40):
        chunks.extend(
            [
                "  console.log",
                f"    debug payload item={i} value={'x' * 48}",
                "",
                f"      at Object.<anonymous> (src/widget.test.js:{10 + i}:13)",
                "",
            ]
        )
    chunks.extend(
        [
            "  ✓ saves (3 ms)",
            "PASS src/other.test.js",
            "  ✓ ok (1 ms)",
            "",
            "stdout | src/other.test.js > ok",
            "vitest live stdout " + ("y" * 60),
            "vitest live stdout more " + ("y" * 40),
            "",
            "FAIL src/payment.test.js",
            "  ● charge › times out",
            "",
            "    expect(received).toBe(expected)",
            "",
            '    Expected: "SUCCESS"',
            '    Received: "GATEWAY_TIMEOUT"',
            "",
            "      42 |   expect(result).toBe('SUCCESS');",
            "",
            "  console.error",
            "    failure side channel must stay",
            "",
            "      at Object.<anonymous> (src/payment.test.js:50:13)",
            "",
            "Test Suites: 1 failed, 2 passed, 3 total",
            "Tests:       1 failed, 3 passed, 4 total",
        ]
    )
    raw = "\n".join(chunks)
    compact = filter_jest_vitest(raw)

    assert "debug payload item=0" not in compact
    assert "vitest live stdout" not in compact
    assert "console lines omitted" in compact
    assert "GATEWAY_TIMEOUT" in compact
    assert "failure side channel must stay" in compact
    assert "FAIL src/payment.test.js" in compact
    raw_tokens = count_tokens(raw).openai
    out_tokens = count_tokens(compact).openai
    reduction = 100 * (raw_tokens - out_tokens) / raw_tokens
    assert reduction >= 85.0, (
        f"expected >=85% savings on console-heavy npm test, got {reduction:.1f}% "
        f"({raw_tokens}->{out_tokens})"
    )


def test_filter_jest_vitest_console_error_under_pass_does_not_freeze_filter():
    """console.error must not trip the diagnostic early-exit (\\bError\\b)."""
    raw = "\n".join(
        [
            "PASS src/a.test.js",
            "  ✓ one (1 ms)",
            "  console.error",
            "    noisy error channel under a passing test " + ("z" * 40),
            "",
            "      at Object.<anonymous> (src/a.test.js:4:5)",
            "",
            "PASS src/b.test.js",
            "  ✓ two (1 ms)",
            "",
            "Test Suites: 2 passed, 2 total",
            "Tests:       2 passed, 2 total",
        ]
    )
    compact = filter_jest_vitest(raw)
    assert "noisy error channel under a passing test" not in compact
    assert "console lines omitted" in compact
    assert "Test Suites: 2 passed, 2 total" in compact


def test_filter_jest_vitest_keeps_failures_and_diffs_verbatim():
    compact = filter_jest_vitest(SAMPLE_JEST_FAIL)

    assert "FAIL src/utils/parser.test.js" in compact
    assert "expect(received).toThrow()" in compact
    assert "Expected substring:" in compact
    assert "Test Suites: 1 failed, 1 passed, 2 total" in compact


def test_filter_jest_vitest_collapses_tests_named_with_diagnostic_words():
    raw = "\n".join(
        [
            " ✓ src/errors.test.ts (2 tests) 5ms",
            "   ✓ handles timeout error properly (2ms)",
            "   ✓ reports warning on failure code (1ms)",
            "",
            "Tests  2 passed (2)",
        ]
    )
    compact = filter_jest_vitest(raw)
    assert "[UsageTrim: 2 passing tests in 1 file, 3 progress records]" in compact
    assert "Tests  2 passed (2)" in compact


def test_auto_specialize_routes_jest_and_vitest():
    compact_vitest = auto_specialize_command_output("npx vitest run", SAMPLE_VITEST_PASS)
    assert compact_vitest is not None
    assert "[UsageTrim: 7 passing tests in 2 files, 9 progress records]" in compact_vitest

    compact_npm = auto_specialize_command_output("npm test -- --coverage", SAMPLE_VITEST_PASS)
    assert compact_npm is not None
    assert "[UsageTrim: 7 passing tests in 2 files, 9 progress records]" in compact_npm


def test_filter_tsc_compacts_errors_and_strips_squiggles():
    compact = filter_tsc(SAMPLE_TSC_ERRORS)

    # Dense: file, line, rule id, message — no pretty frames or duplicate dumps.
    assert "src/auth/jwt.ts:45:12 TS2345" in compact
    assert "Argument of type 'string | undefined'" in compact
    assert "TS2741" in compact
    assert "TS2322" in compact
    assert "Found 6 errors in 3 files." in compact
    assert "~~~~~" not in compact
    assert "verifyToken(headerToken);" not in compact
    assert "const u: User" not in compact
    assert "Errors  Files" not in compact
    # Duplicate pretty blocks collapse to one row per unique diagnostic.
    assert compact.count("src/auth/jwt.ts:45:12 TS2345") == 1
    assert compact.count("src/api/billing.ts:88:3 TS2322") == 1
    before = count_tokens(SAMPLE_TSC_ERRORS).claude
    after = count_tokens(compact).claude
    assert after < before * 0.4, f"expected large drop, got {before} -> {after}"


def test_filter_tsc_clean_output_untouched():
    clean = "✨ Done in 1.42s"
    assert filter_tsc(clean) == clean


def test_auto_specialize_routes_tsc():
    compact = auto_specialize_command_output("npx tsc --noEmit", SAMPLE_TSC_ERRORS)
    assert compact is not None
    assert "~~~~~" not in compact
    assert "TS2345" in compact
    assert "verifyToken(headerToken);" not in compact


def test_filter_json_output_large_array():
    import json

    raw_items = [
        {"id": i, "name": f"item_{i}", "description": "some long description here " * 5}
        for i in range(25)
    ]
    raw_json = json.dumps(raw_items, indent=2)

    slimmed = filter_json_output(raw_json, command="gh api /repos/owner/repo/issues")
    assert slimmed is not None
    assert "Ref: tc_" in slimmed
    assert "omitted" in slimmed
    assert len(slimmed) < len(raw_json) * 0.5


def test_filter_json_output_small_json_untouched():
    raw_json = '{"status": "healthy", "uptime": 12345}'
    assert filter_json_output(raw_json, command="curl http://localhost/health") is None


def test_auto_specialize_routes_json_command():
    import json

    large_payload = json.dumps({"records": [{"id": i, "data": "val"} for i in range(50)]})
    result = auto_specialize_command_output("docker inspect container-1", large_payload)
    assert result is not None
    assert "omitted" in result
    assert "Ref: tc_" in result


def test_filter_cargo_build_collapses_crates():
    crates = "\n".join([f"   Compiling crate_{i} v0.{i}.0" for i in range(20)])
    raw = f"{crates}\nwarning: unused variable `x`\n --> src/main.rs:5:9\n    Finished dev [unoptimized + debuginfo] in 3.12s\n"
    res = filter_cargo_build(raw)
    assert "[UsageTrim: compiled/checked 20 crates]" in res
    assert "warning: unused variable `x`" in res
    assert "Finished dev" in res
    assert "Compiling crate_0" not in res


def test_filter_cargo_build_preserves_error():
    raw = (
        "   Compiling crate_a v0.1.0\n"
        "   Compiling crate_b v0.2.0\n"
        "   Compiling crate_c v0.3.0\n"
        "error[E0425]: cannot find value `foo` in this scope\n"
        "  --> src/main.rs:10:5\n"
        "   |\n"
        "10 |     foo();\n"
        "   |     ^^^ not found\n"
    )
    res = filter_cargo_build(raw)
    assert "[UsageTrim: compiled/checked 3 crates]" in res
    assert "error[E0425]: cannot find value `foo` in this scope" in res
    assert "10 |     foo();" in res


def test_filter_pip_install_collapses_progress_and_downloads():
    raw = (
        "Collecting requests>=2.31.0\n"
        "  Downloading requests-2.31.0-py3-none-any.whl (62 kB)\n"
        "     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 62.6/62.6 kB 8.2 MB/s eta 0:00:00\n"
        "Collecting urllib3<3,>=1.21.1\n"
        "  Downloading urllib3-2.2.1-py3-none-any.whl (121 kB)\n"
        "     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 121.1/121.1 kB 12.1 MB/s eta 0:00:00\n"
        "Installing collected packages: urllib3, requests\n"
        "Successfully installed requests-2.31.0 urllib3-2.2.1\n"
    )
    res = filter_pip_install(raw)
    assert "[UsageTrim: resolved/downloaded 2 packages]" in res
    assert "Successfully installed requests-2.31.0 urllib3-2.2.1" in res
    assert "━━━━━━━━━━━━━━━━" not in res


def test_filter_uv_project_collapses_package_list_and_debug():
    pkg_lines = "\n".join(f" + pkg{i}==1.0.{i}" for i in range(12))
    raw = (
        "DEBUG Registry requirement already cached: rich==15.0.0\n"
        "DEBUG Sending fresh GET request for: https://example.com/black.whl\n"
        "Using CPython 3.12.11\n"
        "Resolved 16 packages in 1.00s\n"
        "Downloading black (1.7MiB)\n"
        " Downloaded black\n"
        "Prepared 1 package in 11.32s\n"
        "Installed 15 packages in 77ms\n"
        f"{pkg_lines}\n"
        "warning: The package `legacy` is deprecated\n"
    )
    res = filter_uv_project(raw)
    assert "DEBUG " not in res
    assert "Downloading black" not in res
    assert "Downloaded black" not in res
    assert "Resolved 16 packages in 1.00s" in res
    assert "Installed 15 packages in 77ms" in res
    assert "[UsageTrim: +12 package changes collapsed]" in res
    assert " + pkg0==1.0.0" not in res
    assert "warning: The package `legacy` is deprecated" in res

    tiny = "Resolved 2 packages in 12ms\nInstalled 1 package in 2ms\n + click==8.5.0\n"
    tiny_res = filter_uv_project(tiny)
    assert " + click==8.5.0" in tiny_res
    assert "collapsed" not in tiny_res


def test_auto_specialize_routes_uv_project_not_uv_pip_install():
    noisy = "Resolved 10 packages in 1s\n" + "\n".join(f" + p{i}==1.0" for i in range(8))
    sync = auto_specialize_command_output("uv sync --all-extras", noisy)
    assert sync is not None
    assert "[UsageTrim: +8 package changes collapsed]" in sync

    add = auto_specialize_command_output("uv add httpx", noisy)
    assert add is not None
    assert "collapsed" in add

    # ``uv pip install`` stays on the pip specializer path.
    pip_raw = "Collecting foo\nSuccessfully installed foo-1.0.0\n"
    pip_res = auto_specialize_command_output("uv pip install foo", pip_raw)
    assert pip_res is not None
    assert "Successfully installed foo-1.0.0" in pip_res
    assert "collapsed" not in pip_res


def test_filter_npm_install_collapses_deprecations():
    deprecations = "\n".join(
        [f"npm warn deprecated pkg_{i}@1.0.0: version is deprecated" for i in range(8)]
    )
    raw = (
        f"{deprecations}\n"
        "added 185 packages, and audited 186 packages in 2s\n"
        "12 packages are looking for funding\n"
        "  run `npm fund`\n"
        "found 0 vulnerabilities\n"
    )
    res = filter_npm_install(raw)
    assert "[UsageTrim: 8 package deprecation warnings collapsed]" in res
    assert "added 185 packages, and audited 186 packages in 2s" in res
    assert "found 0 vulnerabilities" in res
    assert "run `npm fund`" not in res


def test_auto_specialize_routes_build_and_package_commands():
    cargo_raw = "   Compiling a v1.0.0\n   Compiling b v1.0.0\n   Compiling c v1.0.0\n"
    res_cargo = auto_specialize_command_output("cargo build --release", cargo_raw)
    assert res_cargo is not None
    assert "[UsageTrim: compiled/checked 3 crates]" in res_cargo

    pip_raw = "Collecting foo\nSuccessfully installed foo-1.0.0\n"
    res_pip = auto_specialize_command_output("pip install foo", pip_raw)
    assert res_pip is not None
    assert "Successfully installed foo-1.0.0" in res_pip

    npm_raw = "npm warn deprecated a\nnpm warn deprecated b\nnpm warn deprecated c\nadded 10 pkgs\n"
    res_npm = auto_specialize_command_output("npm install", npm_raw)
    assert res_npm is not None
    assert "[UsageTrim: 3 package deprecation warnings collapsed]" in res_npm


def test_filter_jest_vitest_does_not_inflate_counts_with_file_records():
    # A per-file record already covers the individual records printed beneath it.
    raw = "\n".join(
        [f" ✓ src/mod{i}/unit.test.ts ({10 + i} tests) {12 + i}ms" for i in range(20)]
        + ["", " Test Files  20 passed (20)", "      Tests  390 passed (390)"]
    )

    compact = filter_jest_vitest(raw)

    assert "[UsageTrim: 390 passing tests in 20 files, 20 progress records]" in compact
    assert "Tests  390 passed (390)" in compact


def test_filter_jest_vitest_reports_files_when_test_counts_are_unstated():
    # Jest suite headers state no per-file count, so none may be invented.
    raw = "PASS src/a.test.ts\nPASS src/b.test.ts\nPASS src/c.test.ts\n\nDone\n"

    compact = filter_jest_vitest(raw)

    assert "[UsageTrim: 3 passing test files, 3 progress records]" in compact


@pytest.mark.parametrize(
    "command",
    ["npm run test", "pnpm run test", "yarn run test", "bun run test", "npm run test:unit"],
)
def test_auto_specialize_routes_npm_run_test_forms(command):
    compact = auto_specialize_command_output(command, SAMPLE_VITEST_PASS)

    assert compact is not None
    assert "passing tests in 2 files" in compact


def test_filter_git_diff_folds_lockfiles_and_keeps_code_hunks():
    compact = filter_git_diff(SAMPLE_GIT_DIFF)

    assert "diff --git a/src/main.py b/src/main.py" in compact
    assert "+    new_important_logic()" in compact
    assert "diff --git a/uv.lock b/uv.lock" in compact
    assert "lines of lockfile/generated diff omitted by usagetrim" in compact
    assert "+ extra_lock_line_50" not in compact
    assert count_tokens(compact).claude < count_tokens(SAMPLE_GIT_DIFF).claude


def test_filter_git_diff_leaves_non_diff_output_unchanged():
    raw = "commit abc123\nAuthor: Alice\n\n    just a message\n"
    assert filter_git_diff(raw) == raw


def test_auto_specialize_routes_git_diff_and_show():
    compact_diff = auto_specialize_command_output("git diff HEAD~1", SAMPLE_GIT_DIFF)
    assert compact_diff is not None
    assert "omitted by usagetrim" in compact_diff

    compact_show = auto_specialize_command_output("git show abc1234", SAMPLE_GIT_DIFF)
    assert compact_show is not None
    assert "+    new_important_logic()" in compact_show


def test_filter_ruff_compacts_full_frames_and_keeps_codes():
    compact = filter_ruff(SAMPLE_RUFF_FULL)

    assert "src/auth/session.py:12:8: F401 [*] `os` imported but unused" in compact
    assert "help: Remove unused import: `os`" in compact
    assert (
        "src/auth/session.py:44:5: F841 Local variable `token` is assigned to but never used"
        in compact
    )
    assert "Found 2 errors." in compact
    assert "[*] 1 fixable with the `--fix` option." in compact
    # Source frames and caret underlines are the noise.
    assert "import os" not in compact
    assert "^^^^^" not in compact
    assert count_tokens(compact).claude < count_tokens(SAMPLE_RUFF_FULL).claude


def test_filter_ruff_leaves_clean_output_unchanged():
    clean = "All checks passed!\n"
    assert filter_ruff(clean) == clean


def test_auto_specialize_routes_ruff():
    compact = auto_specialize_command_output("uv run ruff check .", SAMPLE_RUFF_FULL)
    assert compact is not None
    assert "F401" in compact
    assert "import os" not in compact

    compact_direct = auto_specialize_command_output("ruff check src", SAMPLE_RUFF_FULL)
    assert compact_direct is not None
    assert "F841" in compact_direct


def test_filter_mypy_compacts_pretty_frames_and_keeps_codes():
    compact = filter_mypy(SAMPLE_MYPY_PRETTY)

    assert 'src/auth/session.py:12: error: Name "os" is not defined  [name-defined]' in compact
    assert (
        "src/auth/session.py:44: error: Incompatible return value type "
        '(got "None", expected "str")  [return-value]'
    ) in compact
    assert "[union-attr]" in compact
    assert "[var-annotated]" in compact
    assert "Found 8 errors in 4 files (checked 24 source files)" in compact
    assert 'note: Error code "return-value" not covered by "type: ignore" comment' in compact
    assert "print(os.getcwd())" not in compact
    assert "json.loads(raw)" not in compact
    assert "self.cache = {}" not in compact
    before = count_tokens(SAMPLE_MYPY_PRETTY).openai
    after = count_tokens(compact).openai
    assert after < before
    assert (before - after) / before >= 0.45


def test_filter_mypy_leaves_clean_output_unchanged():
    clean = "Success: no issues found in 12 source files\n"
    assert filter_mypy(clean) == clean


def test_auto_specialize_routes_mypy():
    compact = auto_specialize_command_output("uv run mypy src", SAMPLE_MYPY_PRETTY)
    assert compact is not None
    assert "[name-defined]" in compact
    assert "print(os.getcwd())" not in compact

    compact_module = auto_specialize_command_output("python -m mypy .", SAMPLE_MYPY_PRETTY)
    assert compact_module is not None
    assert "[return-value]" in compact_module


def test_filter_docker_build_collapses_progress_and_keeps_failure():
    compact = filter_docker_build(SAMPLE_DOCKER_BUILD_FAIL)

    assert "failed to solve" in compact
    assert "No matching distribution found for broken==9.9.9" in compact
    assert " > [stage-0 24/24] RUN pip install broken==9.9.9:" in compact
    assert "docker build progress lines" in compact
    # Layer chatter is the noise.
    assert "Collecting pkg1-dep0" not in compact
    assert "transferring context:" not in compact
    raw_tokens = count_tokens(SAMPLE_DOCKER_BUILD_FAIL).claude
    compact_tokens = count_tokens(compact).claude
    assert compact_tokens < raw_tokens * 0.05
    assert raw_tokens - compact_tokens > 10_000


def test_filter_docker_build_keeps_success_tags():
    compact = filter_docker_build(SAMPLE_DOCKER_BUILD_OK)

    assert "Successfully tagged app:latest" in compact
    assert "writing image sha256:abcdef0123456789" in compact
    assert "naming to docker.io/library/app:latest done" in compact
    assert "RUN echo ok_3" not in compact
    assert "docker build progress lines" in compact


def test_auto_specialize_routes_docker_build():
    compact = auto_specialize_command_output("docker build -t app .", SAMPLE_DOCKER_BUILD_FAIL)
    assert compact is not None
    assert "failed to solve" in compact
    assert "Collecting pkg1-dep0" not in compact

    compact_buildx = auto_specialize_command_output(
        "docker buildx build --load .", SAMPLE_DOCKER_BUILD_FAIL
    )
    assert compact_buildx is not None
    assert "No matching distribution found" in compact_buildx


def test_filter_pyright_compacts_source_frames_and_keeps_diagnostics():
    compact = filter_pyright(SAMPLE_PYRIGHT)

    assert "reportGeneralTypeIssues" in compact
    assert "reportArgumentType" in compact
    assert "58 errors, 0 warnings, 0 informations" in compact
    assert "token_1: str = maybe_token_1" not in compact
    assert "lookup_user_1(1)" not in compact
    assert "~~~~~" not in compact
    raw_tokens = count_tokens(SAMPLE_PYRIGHT).claude
    compact_tokens = count_tokens(compact).claude
    assert compact_tokens < raw_tokens * 0.6
    assert raw_tokens - compact_tokens > 1_500


def test_filter_pyright_leaves_clean_output_unchanged():
    clean = "0 errors, 0 warnings, 0 informations\n"
    assert filter_pyright(clean) == clean


def test_auto_specialize_routes_pyright_and_basedpyright():
    compact = auto_specialize_command_output("npx pyright", SAMPLE_PYRIGHT)
    assert compact is not None
    assert "reportGeneralTypeIssues" in compact
    assert "token_1: str = maybe_token_1" not in compact

    compact_based = auto_specialize_command_output("basedpyright src", SAMPLE_PYRIGHT)
    assert compact_based is not None
    assert "58 errors, 0 warnings" in compact_based


def test_filter_eslint_compacts_stylish_into_dense_lines():
    compact = filter_eslint(SAMPLE_ESLINT_STYLISH)

    assert (
        "src/auth/session.ts:12:8 error 'os' is defined but never used "
        "@typescript-eslint/no-unused-vars" in compact
    )
    assert (
        "src/auth/session.ts:44:5 warning Unexpected any. Specify a different type "
        "@typescript-eslint/no-explicit-any" in compact
    )
    assert (
        "src/utils/helpers.ts:3:1 error Prefer named exports import/prefer-default-export"
        in compact
    )
    assert "src/api/billing.ts:55:3 error Expected '!==' and instead saw '!=' eqeqeq" in compact
    assert "@typescript-eslint/no-unused-vars" in compact
    assert "✖ 11 problems (8 errors, 3 warnings)" in compact
    # Absolute path headers, padding, fixable footer, and duplicate blocks are noise.
    assert "/Users/dev/acme/" not in compact
    assert "potentially fixable" not in compact
    assert re.search(r"^\s+\d+:\d+\s+error", compact, re.MULTILINE) is None
    assert compact.count("src/auth/session.ts:12:8 error") == 1
    before = count_tokens(SAMPLE_ESLINT_STYLISH).claude
    after = count_tokens(compact).claude
    assert after < before * 0.55, f"expected large drop, got {before} -> {after}"


def test_filter_eslint_compacts_codeframe_and_strips_source():
    compact = filter_eslint(SAMPLE_ESLINT_CODEFRAME)

    assert (
        "src/auth/session.ts:12:8 error 'os' is defined but never used "
        "(@typescript-eslint/no-unused-vars)" in compact
    )
    assert "@typescript-eslint/no-explicit-any" in compact
    assert "import/prefer-default-export" in compact
    assert "✖ 5 problems (5 errors, 0 warnings)" in compact
    assert "import os from" not in compact
    assert "const token: any" not in compact
    assert "at Module._compile" not in compact
    assert "at processTicksAndRejections" not in compact
    assert compact.count("src/auth/session.ts:12:8 error") == 1
    assert re.search(r"^\s*[|^\s]+$", compact, re.MULTILINE) is None
    before = count_tokens(SAMPLE_ESLINT_CODEFRAME).claude
    after = count_tokens(compact).claude
    assert after < before * 0.35, f"expected large drop, got {before} -> {after}"


def test_filter_eslint_leaves_clean_output_unchanged():
    clean = "✨  Done in 1.12s\n"
    assert filter_eslint(clean) == clean


def test_auto_specialize_routes_eslint():
    compact = auto_specialize_command_output("npx eslint src --ext .ts", SAMPLE_ESLINT_STYLISH)
    assert compact is not None
    assert "src/auth/session.ts:12:8 error" in compact
    assert "/Users/dev/acme/" not in compact

    compact_direct = auto_specialize_command_output("eslint .", SAMPLE_ESLINT_CODEFRAME)
    assert compact_direct is not None
    assert "src/auth/session.ts:12:8 error" in compact_direct
    assert "import os from" not in compact_direct
    assert "at Module._compile" not in compact_direct

    compact_pnpm = auto_specialize_command_output(
        "pnpm eslint . --format codeframe", SAMPLE_ESLINT_CODEFRAME
    )
    assert compact_pnpm is not None
    assert "@typescript-eslint/no-unused-vars" in compact_pnpm


def test_filter_kubectl_describe_keeps_failure_signal_drops_noise():
    compact = filter_kubectl(SAMPLE_KUBECTL_DESCRIBE)

    assert "Name:             api-7d8f9c-xk2m9" in compact
    assert "Namespace:        production" in compact
    assert "Status:           Running" in compact
    assert "CrashLoopBackOff" in compact
    assert "Restart Count:  12" in compact
    assert "Ready:          False" in compact
    assert "Back-off restarting failed container" in compact
    assert "Liveness probe failed" in compact
    assert "last-applied-configuration" not in compact
    assert "CFG_VAR_0:" not in compact
    assert "Successfully assigned production/api-7d8f9c-0000" not in compact
    assert "node.kubernetes.io/not-ready" not in compact
    assert "routine tolerations collapsed" in compact
    assert "annotations" in compact.lower()
    assert "env" in compact.lower() or "Environment" in compact
    before = count_tokens(SAMPLE_KUBECTL_DESCRIBE).openai
    after = count_tokens(compact).openai
    assert after < before * 0.25
    assert before - after > 2_000


def test_filter_kubectl_get_collapses_healthy_rows():
    compact = filter_kubectl(SAMPLE_KUBECTL_GET)

    assert "CrashLoopBackOff" in compact
    assert "Pending" in compact
    assert "worker-crash-aaaa" in compact
    assert "worker-pending-bbbb" in compact
    assert "api-7d8f9c-0000" not in compact
    assert "Running" in compact  # summary mentions healthy Running count
    before = count_tokens(SAMPLE_KUBECTL_GET).openai
    after = count_tokens(compact).openai
    assert after < before * 0.35


KUBECTL_GET_HEADER = "NAME                READY   STATUS             RESTARTS      AGE"


def _kubectl_get(*rows: str) -> str:
    return "\n".join([KUBECTL_GET_HEADER, *rows])


def test_filter_kubectl_get_keeps_pods_that_are_restarting():
    # A pod can report Running while restarting constantly; that is the signal.
    raw = _kubectl_get(
        "api-aaaa            1/1     Running            0             5d",
        "payments-bbbb       1/1     Running            47 (5m ago)   2d",
    )

    compact = filter_kubectl(raw)

    assert "payments-bbbb" in compact
    assert "47 (5m ago)" in compact
    assert "api-aaaa" not in compact


def test_filter_kubectl_get_keeps_partially_ready_pods():
    # 1/2 ready is a degraded pod, not a healthy one.
    raw = _kubectl_get(
        "api-aaaa            2/2     Running            0             5d",
        "search-bbbb         1/2     Running            0             3d",
    )

    compact = filter_kubectl(raw)

    assert "search-bbbb" in compact
    assert "api-aaaa" not in compact


def test_filter_kubectl_get_collapses_finished_pods():
    # Finished pods report 0/N ready by design, so readiness must not keep them.
    raw = _kubectl_get(
        "backup-aaaa         0/1     Completed          0             1h",
        "backup-bbbb         0/2     Completed          0             2h",
        "restore-cccc        0/1     Succeeded          0             3h",
    )

    compact = filter_kubectl(raw)

    assert "[UsageTrim: 3 healthy Running/Completed rows collapsed]" in compact
    assert "backup-aaaa" not in compact
    assert "restore-cccc" not in compact


def test_filter_kubectl_get_keeps_rows_with_an_unreadable_restart_column():
    raw = _kubectl_get("api-aaaa            1/1     Running            <unknown>     5d")

    assert "api-aaaa" in filter_kubectl(raw)


def test_filter_kubectl_logs_collapses_probes_keeps_errors():
    compact = filter_kubectl(SAMPLE_KUBECTL_LOGS)

    assert "health/ready probe lines collapsed" in compact
    assert "jwt verification failed" in compact
    assert "AuthError: token expired" in compact
    assert "Liveness probe failed" in compact
    assert "GET /healthz 500" in compact
    assert compact.count("GET /readyz") < 5
    before = count_tokens(SAMPLE_KUBECTL_LOGS).openai
    after = count_tokens(compact).openai
    assert after < before * 0.35
    # Idempotent on compacted + short input.
    assert filter_kubectl(compact) == compact
    short = "pod started\n"
    assert filter_kubectl(short) == short


def test_auto_specialize_routes_kubectl():
    compact = auto_specialize_command_output(
        "kubectl -n production describe pod api-7d8f9c-xk2m9", SAMPLE_KUBECTL_DESCRIBE
    )
    assert compact is not None
    assert "CrashLoopBackOff" in compact
    assert "last-applied-configuration" not in compact

    compact_get = auto_specialize_command_output("kubectl get pods -o wide", SAMPLE_KUBECTL_GET)
    assert compact_get is not None
    assert "CrashLoopBackOff" in compact_get
    assert "api-7d8f9c-0000" not in compact_get

    compact_logs = auto_specialize_command_output(
        "kubectl logs api-7d8f9c-xk2m9 --all-containers", SAMPLE_KUBECTL_LOGS
    )
    assert compact_logs is not None
    assert "health/ready probe lines collapsed" in compact_logs
    assert "AuthError: token expired" in compact_logs

    compact_oc = auto_specialize_command_output("oc get pods -n production", SAMPLE_KUBECTL_GET)
    assert compact_oc is not None
    assert "CrashLoopBackOff" in compact_oc


def test_filter_helm_folds_packaging_keeps_notes_and_status():
    compact = filter_helm(SAMPLE_HELM_INSTALL)

    assert "NAME: api" in compact
    assert "STATUS: deployed" in compact
    assert "REVISION: 4" in compact
    assert "NAMESPACE: production" in compact
    assert "NOTES:" in compact
    assert "kubectl port-forward" in compact
    assert "Warning: chart appVersion differs from image tag" in compact
    assert "chart/repo lines collapsed" in compact
    assert "Hang tight while we grab" not in compact
    assert "Saving 5 charts" not in compact
    assert "Deleting outdated charts" not in compact
    before = count_tokens(SAMPLE_HELM_INSTALL).openai
    after = count_tokens(compact).openai
    assert after < before * 0.55
    assert filter_helm(compact) == compact
    short = "STATUS: deployed\n"
    assert filter_helm(short) == short


def test_auto_specialize_routes_helm():
    compact = auto_specialize_command_output(
        "helm upgrade --install api ./charts/api -n production", SAMPLE_HELM_INSTALL
    )
    assert compact is not None
    assert "STATUS: deployed" in compact
    assert "NOTES:" in compact
    assert "Hang tight while we grab" not in compact


def test_filter_terraform_plan_collapses_refresh_keeps_plan():
    compact = filter_terraform_plan(SAMPLE_TERRAFORM_PLAN)

    assert "Plan: 1 to add, 1 to change, 0 to destroy." in compact
    assert 'resource "aws_instance" "api"' in compact
    assert "# aws_instance.api will be updated in-place" in compact
    assert "# aws_lb_listener_rule.canary will be created" in compact
    assert "t3.small" in compact
    assert "t3.medium" in compact
    assert 'resource "aws_lb_listener_rule" "canary"' in compact
    assert "Warning: Deprecated attribute" in compact
    assert "Refreshing state..." not in compact
    assert "Refreshing..." not in compact
    assert "Reading..." not in compact
    assert "Read complete after" not in compact
    assert "refresh/read lines collapsed" in compact
    assert "unchanged attributes folded" in compact
    assert "ami-0abcdef1234567890" not in compact
    assert "associate_public_ip_address" not in compact
    # Alias stays wired.
    assert filter_terraform(SAMPLE_TERRAFORM_PLAN) == compact
    before = count_tokens(SAMPLE_TERRAFORM_PLAN).openai
    after = count_tokens(compact).openai
    assert after < before * 0.2
    assert before - after > 1_500


def test_filter_terraform_plan_idempotent_on_short_and_compacted():
    short = "Plan: 0 to add, 0 to change, 0 to destroy.\n"
    assert filter_terraform_plan(short) == short
    once = filter_terraform_plan(SAMPLE_TERRAFORM_PLAN)
    assert filter_terraform_plan(once) == once


def test_filter_terraform_plan_opentofu_wording():
    raw = "\n".join(
        [
            "aws_s3_bucket.data: Refreshing state... [id=bucket]",
            "module.vpc.aws_subnet.a: Refreshing...",
            "data.aws_ami.ubuntu: Reading...",
            "data.aws_ami.ubuntu: Read complete after 1s [id=ami-1]",
            "",
            "OpenTofu will perform the following actions:",
            "",
            "  # aws_s3_bucket.logs will be created",
            '  + resource "aws_s3_bucket" "logs" {',
            '      + bucket = "app-logs"',
            "    }",
            "",
            "Plan: 1 to add, 0 to change, 0 to destroy.",
            "",
            "Error: Invalid configuration",
            "│ on main.tf line 1",
        ]
    )
    compact = filter_terraform_plan(raw)
    assert "OpenTofu will perform the following actions:" in compact
    assert "# aws_s3_bucket.logs will be created" in compact
    assert "Plan: 1 to add, 0 to change, 0 to destroy." in compact
    assert "Error: Invalid configuration" in compact
    assert "Refreshing state..." not in compact
    assert "Reading..." not in compact
    assert "refresh/read lines collapsed" in compact


def test_auto_specialize_routes_terraform_and_tofu():
    compact = auto_specialize_command_output("terraform plan -out=tfplan", SAMPLE_TERRAFORM_PLAN)
    assert compact is not None
    assert "Plan: 1 to add, 1 to change, 0 to destroy." in compact
    assert "Refreshing state..." not in compact

    compact_tofu = auto_specialize_command_output("tofu plan", SAMPLE_TERRAFORM_PLAN)
    assert compact_tofu is not None
    assert "aws_instance" in compact_tofu

    for command in (
        "terraform apply -auto-approve",
        "tofu destroy -auto-approve",
        "/usr/local/bin/terraform plan",
        "terraform.exe plan",
    ):
        routed = auto_specialize_command_output(command, SAMPLE_TERRAFORM_PLAN)
        assert routed is not None, command
        assert "Plan: 1 to add, 1 to change, 0 to destroy." in routed

    assert auto_specialize_command_output("terraform version", SAMPLE_TERRAFORM_PLAN) is None
    assert auto_specialize_command_output("terraform fmt", SAMPLE_TERRAFORM_PLAN) is None


def test_compress_to_budget():
    large_text = "This is a sentence that has some words and will be repeated many times. " * 80
    initial_tokens = count_tokens(large_text).claude
    assert initial_tokens > 200

    # Request strict budget of 50 tokens
    budget_fitted = compress_to_budget(large_text, max_tokens=60, provider="claude")
    fitted_tokens = count_tokens(budget_fitted).claude

    assert fitted_tokens <= 60
    assert "Ref: tc_" in budget_fitted


@pytest.mark.parametrize("provider", ["claude", "openai", "gemini"])
@pytest.mark.parametrize("budget", [64, 100, 500])
@pytest.mark.parametrize(
    "raw", ["word " * 3000, "漢字🙂é" * 2000, "\n".join(f"step {i}" for i in range(300))]
)
def test_budget_includes_reference_and_suffix(provider, budget, raw):
    output = compress_to_budget(raw, budget, provider, suffix="\n[exit code: 1]")
    assert getattr(count_tokens(output), provider) <= budget
    assert output.endswith("[exit code: 1]")
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert ContextCache().retrieve(ref) == raw


def test_budget_redacts_even_when_input_fits():
    secret = "sk-proj-" + "a" * 30
    output = compress_to_budget(f"token={secret}", 100)
    assert secret not in output
    assert "REDACTED" in output


def test_small_budget_cannot_silently_drop_recovery():
    with pytest.raises(ValueError, match="recovery reference"):
        compress_to_budget("long text " * 100, 1)


def test_cleaning_still_returns_recoverable_reference():
    raw = "\x1b[31mred\x1b[0m\n" * 100
    output = compress_to_budget(raw, 100)
    assert count_tokens(output).claude <= 100
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert ContextCache().retrieve(ref) == raw


def test_filter_directory_scan():
    from usagetrim.core.specialized import filter_directory_scan

    raw = """./src
./src/index.ts
./src/app.ts
./node_modules
./node_modules/react
./node_modules/react/index.js
./node_modules/react/package.json
./.git
./.git/HEAD
./.git/config
./package.json
./README.md
"""
    compact = filter_directory_scan(raw)
    assert "./src/index.ts" in compact
    assert "./src/app.ts" in compact
    assert "./package.json" in compact
    assert "./README.md" in compact
    assert "node_modules/ [... 4 items omitted by usagetrim" in compact
    assert ".git/ [... 3 items omitted by usagetrim" in compact
    assert "./node_modules/react/index.js" not in compact


def test_auto_specialize_routes_find_and_tree():
    raw = "\n".join([f"./node_modules/pkg_{i}/file.js" for i in range(20)] + ["./src/main.py"])
    res = auto_specialize_command_output("find . -type f", raw)
    assert res is not None
    assert "./src/main.py" in res
    assert "node_modules/ [... 20 items omitted by usagetrim" in res


def test_filter_git_branch():
    from usagetrim.core.specialized import filter_git_branch

    raw = """* main
  feature/my-local-feature
  remotes/origin/HEAD -> origin/main
  remotes/origin/main
  remotes/origin/dependabot/npm_and_yarn/foo-1.0
  remotes/origin/dependabot/npm_and_yarn/bar-2.0
  remotes/origin/dependabot/npm_and_yarn/baz-3.0
  remotes/origin/dependabot/npm_and_yarn/qux-4.0
  remotes/origin/pr/100
  remotes/origin/pr/101
  remotes/origin/pr/102
"""
    compact = filter_git_branch(raw)
    assert "* main" in compact
    assert "feature/my-local-feature" in compact
    assert "remotes/origin/HEAD -> origin/main" in compact
    assert "remotes/origin/dependabot/* [... 4 branches collapsed ...]" in compact
    assert "remotes/origin/pr/* [... 3 branches collapsed ...]" in compact


def test_filter_curl_http():
    from usagetrim.core.specialized import filter_curl_http

    raw = """*   Trying 127.0.0.1:8000...
* Connected to localhost (127.0.0.1) port 8000 (#0)
> GET /api/v1/health HTTP/1.1
> Host: localhost:8000
> 
< HTTP/1.1 200 OK
< date: Tue, 22 Sep 2026 19:40:00 GMT
< server: uvicorn
< content-type: application/json
< access-control-allow-origin: *
< access-control-allow-credentials: true
< x-process-time: 0.0012
< etag: W/"15-abc"
< keep-alive: timeout=5
< connection: keep-alive
< 
{"status":"ok"}
"""
    compact = filter_curl_http(raw)
    assert "< HTTP/1.1 200 OK" in compact
    assert "content-type: application/json" in compact
    assert '{"status":"ok"}' in compact
    assert "routine response headers collapsed" in compact
    assert "server: uvicorn" not in compact


def _gh_log(job: str, step: str, lines: list[str]) -> str:
    return "\n".join(f"{job}\t{step}\t2026-09-24T12:00:00.0000000Z {line}" for line in lines)


_GH_RUN_LOG = "\n".join(
    [
        _gh_log(
            "lint",
            "Set up job",
            [
                "Current runner version: '2.337.0'",
                "##[group]Runner Image",
                "Image: ubuntu-24.04",
                "Version: 20260907.300",
                "##[endgroup]",
                "Download action repository 'actions/checkout@v4'",
                "Complete job name: lint",
            ],
        ),
        _gh_log(
            "lint",
            "Restore cache",
            [
                "##[group]Run actions/cache/restore@v4",
                "with:",
                "  fail-on-cache-miss: false",
                "  lookup-only: false",
                "env:",
                "  PYTHONPATH: /home/runner/work",
                "##[endgroup]",
                "Cache not found for input keys: uv-",
            ],
        ),
        _gh_log(
            "lint",
            "Check type discipline",
            [f"checked module {n}" for n in range(30)]
            + [
                "FAIL: LIT-rule totals exceed their limit (base c6c3881):",
                "  LIT002: total 26719 over limit 26715 (this change added 10)",
                "    handler.py:30",
                "##[error]Process completed with exit code 1.",
            ],
        ),
        _gh_log(
            "lint",
            "Post job cleanup",
            [
                "[command]/usr/bin/git config --local --unset-all http.extraheader",
                "Removing SSH command configuration",
                "Cleaning up orphan processes",
            ],
        ),
    ]
)


def test_filter_gh_run_log_keeps_failure_details_and_drops_runner_noise(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    from usagetrim.core.cache import ContextCache
    from usagetrim.core.specialized import filter_gh_run_log

    compact = filter_gh_run_log(_GH_RUN_LOG)

    assert compact is not None
    assert "── lint › Check type discipline" in compact
    assert "FAIL: LIT-rule totals exceed their limit" in compact
    assert "LIT002: total 26719 over limit 26715" in compact
    assert "ERROR: Process completed with exit code 1." in compact
    for noise in (
        "Current runner version",
        "fail-on-cache-miss",
        "PYTHONPATH",
        "[command]/usr/bin/git",
        "Removing SSH",
        "2026-09-24T12:00:00",
    ):
        assert noise not in compact
    assert "lines omitted" in compact
    ref_id = compact.split("Ref: ", 1)[1].split("]", 1)[0]
    assert ContextCache().retrieve(ref_id) == _GH_RUN_LOG


def test_gh_run_log_filter_only_applies_to_log_views(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    from usagetrim.core.specialized import auto_specialize_command_output, filter_gh_run_log

    compact = auto_specialize_command_output("gh run view 123 --log-failed", _GH_RUN_LOG)
    assert compact is not None and "LIT002" in compact and "Runner Image" not in compact
    assert auto_specialize_command_output("gh run view 123", _GH_RUN_LOG) is None
    assert filter_gh_run_log("plain text\nwithout any tab-separated gh log prefixes\n" * 20) is None


def _rg_json_event(event_type: str, path: str, **extra) -> str:
    data: dict = {"path": {"text": path}}
    data.update(extra)
    return json.dumps({"type": event_type, "data": data}, separators=(",", ":"))


def _rg_json_match(path: str, line_number: int, text: str, matched: str = "needle") -> str:
    start = text.find(matched)
    if start < 0:
        start = 0
        matched = text[: max(1, min(6, len(text)))]
    return _rg_json_event(
        "match",
        path,
        lines={"text": text if text.endswith("\n") else text + "\n"},
        line_number=line_number,
        absolute_offset=line_number * 40,
        submatches=[{"match": {"text": matched}, "start": start, "end": start + len(matched)}],
    )


def _rg_json_context(path: str, line_number: int, text: str) -> str:
    return _rg_json_event(
        "context",
        path,
        lines={"text": text if text.endswith("\n") else text + "\n"},
        line_number=line_number,
        absolute_offset=line_number * 40,
        submatches=[],
    )


def _sample_rg_json_dense() -> str:
    """Authored ``rg --json`` stream: one hot file, context noise, many other hits."""
    lines: list[str] = []
    hot = "vendor/third_party/bundle.js"
    lines.append(_rg_json_event("begin", hot))
    for i in range(1, 16):
        lines.append(
            _rg_json_context(hot, i * 10 - 1, f"// padding context line {i} " + ("x" * 60))
        )
        lines.append(_rg_json_match(hot, i * 10, f"function needle_helper_{i}() {{ return {i}; }}"))
        lines.append(_rg_json_context(hot, i * 10 + 1, f"// trailing context {i} " + ("y" * 60)))
    lines.append(
        _rg_json_event(
            "end",
            hot,
            binary_offset=None,
            stats={
                "elapsed": {"secs": 0, "nanos": 1},
                "searches": 1,
                "searches_with_match": 1,
                "bytes_searched": 9000,
                "bytes_printed": 8000,
                "matched_lines": 15,
                "matches": 15,
            },
        )
    )
    for file_i in range(20):
        path = f"src/mod_{file_i}/handler.py"
        lines.append(_rg_json_event("begin", path))
        for j in range(4):
            lines.append(_rg_json_context(path, 20 + j * 5 - 1, f"# ctx before {file_i}.{j}"))
            lines.append(_rg_json_match(path, 20 + j * 5, f"def use_needle_{file_i}_{j}(): pass"))
            lines.append(_rg_json_context(path, 20 + j * 5 + 1, f"# ctx after {file_i}.{j}"))
        lines.append(_rg_json_event("end", path, binary_offset=None, stats={}))
    lines.append(
        json.dumps(
            {
                "type": "summary",
                "data": {
                    "elapsed_total": {"human": "0.05s", "nanos": 5e7, "secs": 0},
                    "stats": {"matched_lines": 95, "matches": 95},
                },
            },
            separators=(",", ":"),
        )
    )
    return "\n".join(lines)


SAMPLE_RG_JSON_DENSE = _sample_rg_json_dense()

SAMPLE_RG_TEXT_CONTEXT = "\n".join(
    [
        "src/utils.py-9-# helpers",
        "src/utils.py:10:def needle_a():",
        "src/utils.py-11-    return 1",
        "--",
        "src/utils.py-19-# more",
        "src/utils.py:20:def needle_b():",
        "src/utils.py-21-    return 2",
        "--",
        "src/utils.py-29-# again",
        "src/utils.py:30:def needle_c():",
        "src/utils.py-31-    return 3",
        "--",
        "src/utils.py-39-# again",
        "src/utils.py:40:def needle_d():",
        "src/utils.py-41-    return 4",
        "--",
        "src/app.py-4-import os",
        "src/app.py:5:from utils import needle_a",
        "src/app.py-6-print('ok')",
    ]
)


def test_filter_ripgrep_json_clusters_by_file_and_drops_context():
    compact = filter_ripgrep_output(SAMPLE_RG_JSON_DENSE)

    assert "vendor/third_party/bundle.js: 15 matches (3 shown, 12 omitted by usagetrim)" in compact
    assert "function needle_helper_1()" in compact
    assert "function needle_helper_2()" in compact
    assert "function needle_helper_3()" in compact
    assert "function needle_helper_4()" not in compact
    assert "padding context line" not in compact
    assert "trailing context" not in compact
    assert '"type":"context"' not in compact
    assert '"type":"begin"' not in compact
    assert "src/mod_0/handler.py" in compact
    # Global cap stops flooding the rest of the tree.
    assert "more files omitted by usagetrim" in compact
    assert "src/mod_19/handler.py" not in compact


def test_filter_ripgrep_json_saves_majority_of_tokens():
    compact = filter_ripgrep_output(SAMPLE_RG_JSON_DENSE)
    raw_tokens = count_tokens(SAMPLE_RG_JSON_DENSE).openai
    out_tokens = count_tokens(compact).openai
    reduction = 100 * (raw_tokens - out_tokens) / raw_tokens
    assert reduction >= 70.0, (
        f"expected >=70% savings on rg --json, got {reduction:.1f}% ({raw_tokens}->{out_tokens})"
    )
    assert len(compact) < len(SAMPLE_RG_JSON_DENSE) * 0.3


def test_filter_ripgrep_text_caps_per_file_and_drops_context():
    compact = filter_ripgrep_output(SAMPLE_RG_TEXT_CONTEXT)

    assert "src/utils.py: 4 matches (3 shown, 1 omitted by usagetrim)" in compact
    assert "10: def needle_a():" in compact
    assert "20: def needle_b():" in compact
    assert "30: def needle_c():" in compact
    assert "40: def needle_d():" not in compact
    assert "src/app.py: 1 match" in compact
    assert "5: from utils import needle_a" in compact
    assert "# helpers" not in compact
    assert "src/utils.py-9-" not in compact


def test_filter_ripgrep_leaves_unrelated_output_unchanged():
    raw = "rg: unrecognized flag --nope\n"
    assert filter_ripgrep_output(raw) == raw


def test_auto_specialize_routes_rg_and_grep_r():
    compact = auto_specialize_command_output("rg --json needle", SAMPLE_RG_JSON_DENSE)
    assert compact is not None
    assert "omitted by usagetrim" in compact
    assert "padding context line" not in compact

    compact_abs = auto_specialize_command_output(
        "/opt/homebrew/bin/rg -n needle src", SAMPLE_RG_TEXT_CONTEXT
    )
    assert compact_abs is not None
    assert "src/utils.py: 4 matches" in compact_abs

    compact_grep = auto_specialize_command_output("grep -rn needle .", SAMPLE_RG_TEXT_CONTEXT)
    assert compact_grep is not None
    assert "omitted by usagetrim" in compact_grep

    assert auto_specialize_command_output("grep needle file.txt", SAMPLE_RG_TEXT_CONTEXT) is None


# ---------------------------------------------------------------------------
# CI log folding (GitHub Actions / GitLab / CircleCI banners)
# ---------------------------------------------------------------------------


def _gha_line(ts: str, text: str) -> str:
    return f"{ts} {text}"


_SAMPLE_GHA_CI_LOG = "\n".join(
    [
        _gha_line("2026-09-27T12:01:00.123Z", "##[group]Operating System"),
        *[_gha_line("2026-09-27T12:01:00.124Z", f"os-detail-line-{i}") for i in range(40)],
        _gha_line("2026-09-27T12:01:00.300Z", "##[endgroup]"),
        _gha_line("2026-09-27T12:01:01.000Z", "##[group]Run actions/checkout@v4"),
        *[_gha_line("2026-09-27T12:01:01.010Z", f"git-fetch-{i}") for i in range(30)],
        _gha_line("2026-09-27T12:01:02.000Z", "##[endgroup]"),
        _gha_line("2026-09-27T12:01:05.000Z", "##[group]Run uv run pytest"),
        *[_gha_line("2026-09-27T12:01:05.010Z", f"collected noise {i}") for i in range(50)],
        _gha_line(
            "2026-09-27T12:01:06.000Z",
            "FAILED tests/test_api.py::test_auth - AssertionError: assert 401 == 200",
        ),
        _gha_line("2026-09-27T12:01:06.050Z", "Traceback (most recent call last):"),
        _gha_line("2026-09-27T12:01:06.060Z", '  File "tests/test_api.py", line 10, in test_auth'),
        _gha_line("2026-09-27T12:01:06.100Z", "##[error]Process completed with exit code 1."),
        _gha_line("2026-09-27T12:01:06.200Z", "##[endgroup]"),
        _gha_line("2026-09-27T12:01:07.000Z", "##[group]Post job cleanup"),
        *[_gha_line("2026-09-27T12:01:07.010Z", f"cleanup-{i}") for i in range(25)],
        _gha_line("2026-09-27T12:01:08.000Z", "##[endgroup]"),
    ]
)


_SAMPLE_GITLAB_CI_LOG = "\n".join(
    [
        "section_start:1690000000:get_sources\rget_sources",
        *[f"fetching object {i}" for i in range(35)],
        "section_end:1690000000:get_sources\r",
        "section_start:1690000001:script\rscript",
        *[f"running step {i}" for i in range(40)],
        "FAILED tests/test_api.py::test_auth - AssertionError: assert 401 == 200",
        "Error: process exited with code 1",
        "section_end:1690000001:script\r",
        "section_start:1690000002:cleanup_file_variables\rcleanup_file_variables",
        *[f"cleanup var {i}" for i in range(20)],
        "section_end:1690000002:cleanup_file_variables\r",
    ]
)


_SAMPLE_CIRCLE_CI_LOG = "\n".join(
    [
        "====>> Spin up Environment",
        *[f"starting container layer {i}" for i in range(20)],
        "====>> Checkout code",
        *[f"cloning repo blob {i}" for i in range(20)],
        "====>> Run tests",
        *[f"test noise {i}" for i in range(30)],
        "FAILED tests/test_api.py::test_auth - AssertionError: assert 401 == 200",
        "Error: exit code 1",
    ]
)


def test_filter_ci_logs_folds_github_setup_keeps_failure():
    compact = filter_ci_logs(_SAMPLE_GHA_CI_LOG)
    assert compact != _SAMPLE_GHA_CI_LOG
    assert "folded by usagetrim" in compact
    assert "Operating System" in compact
    assert "actions/checkout@v4" in compact or "checkout" in compact.lower()
    assert "Post job cleanup" in compact
    assert "os-detail-line-0" not in compact
    assert "git-fetch-0" not in compact
    assert "cleanup-0" not in compact
    assert "FAILED tests/test_api.py::test_auth" in compact
    assert "AssertionError: assert 401 == 200" in compact
    assert "##[error]Process completed with exit code 1." in compact
    assert "2026-09-27T12:01:00.123Z" not in compact
    raw_tokens = count_tokens(_SAMPLE_GHA_CI_LOG).openai
    out_tokens = count_tokens(compact).openai
    assert raw_tokens > 0 and out_tokens < raw_tokens * 0.15


def test_filter_ci_logs_folds_gitlab_sections():
    compact = filter_ci_logs(_SAMPLE_GITLAB_CI_LOG)
    assert "folded by usagetrim" in compact
    assert "get_sources" in compact
    assert "cleanup_file_variables" in compact
    assert "fetching object 0" not in compact
    assert "FAILED tests/test_api.py::test_auth" in compact
    assert "AssertionError: assert 401 == 200" in compact


def test_filter_ci_logs_circleci_banners():
    compact = filter_ci_logs(_SAMPLE_CIRCLE_CI_LOG)
    assert "folded by usagetrim" in compact
    assert "Spin up Environment" in compact or "Checkout code" in compact
    assert "starting container layer 0" not in compact
    assert "FAILED tests/test_api.py::test_auth" in compact


def test_filter_ci_logs_idempotent_on_short_and_folded():
    short = "hello\nworld\n"
    assert filter_ci_logs(short) == short
    once = filter_ci_logs(_SAMPLE_GHA_CI_LOG)
    assert filter_ci_logs(once) == once


def test_auto_specialize_routes_glab_and_content_ci_logs():
    via_glab = auto_specialize_command_output("glab ci trace 42", _SAMPLE_GHA_CI_LOG)
    assert via_glab is not None
    assert "FAILED tests/test_api.py::test_auth" in via_glab
    assert "folded by usagetrim" in via_glab

    via_circle = auto_specialize_command_output("circleci step logs", _SAMPLE_CIRCLE_CI_LOG)
    assert via_circle is not None
    assert "FAILED tests/test_api.py::test_auth" in via_circle

    # Content detection when markers are present (e.g. cat of a downloaded log).
    via_content = auto_specialize_command_output("cat /tmp/job.log", _SAMPLE_GITLAB_CI_LOG)
    assert via_content is not None
    assert "folded by usagetrim" in via_content

# ---------------------------------------------------------------------------
# SQL / ORM query log compaction (Prisma, Django, SQLAlchemy, Drizzle)
# ---------------------------------------------------------------------------


def _django_n_plus_one(n: int = 50) -> str:
    lines = [
        (
            f'[django.db.backends] (0.001) SELECT "users"."id", "users"."name" '
            f'FROM "users" WHERE "users"."id" = {i}; args=({i},)'
        )
        for i in range(1, n + 1)
    ]
    lines.append(
        "django.db.utils.IntegrityError: duplicate key value violates unique "
        'constraint "users_email_key"'
    )
    return "\n".join(lines)


def _sqlalchemy_tx_and_queries(n: int = 12) -> str:
    lines = [
        "INFO sqlalchemy.engine.Engine BEGIN (implicit)",
        "INFO sqlalchemy.engine.Engine Connection <Connection> checked out from pool",
    ]
    for i in range(1, n + 1):
        lines.append(
            f"INFO sqlalchemy.engine.Engine SELECT users.id FROM users WHERE users.id = {i}"
        )
        lines.append(f"INFO sqlalchemy.engine.Engine [generated in 0.0001s] ({i},)")
    lines.extend(
        [
            "INFO sqlalchemy.engine.Engine COMMIT",
            "INFO sqlalchemy.engine.Engine BEGIN (implicit)",
            "INFO sqlalchemy.engine.Engine ROLLBACK",
            "FAILED tests/test_models.py::test_create - AssertionError: assert 0 == 1",
        ]
    )
    return "\n".join(lines)


def _prisma_query_spam(n: int = 20) -> str:
    lines = [
        f"prisma:query SELECT `main`.`User`.`id` FROM `main`.`User` WHERE `main`.`User`.`id` = {i}"
        for i in range(1, n + 1)
    ]
    lines.append("PrismaClientKnownRequestError: Unique constraint failed on the fields: (`email`)")
    return "\n".join(lines)


_SAMPLE_DRIZZLE_QUERIES = "\n".join(
    [
        'Query: select "id", "email" from "users" where "id" = 1',
        'Query: select "id", "email" from "users" where "id" = 2',
        'Query: select "id", "email" from "users" where "id" = 3',
        'Query: select "id", "email" from "users" where "id" = 4',
        'Query: select "id", "email" from "users" where "id" = 5',
        "Error: Failed query: insert into users",
    ]
)


_SAMPLE_DDL_AND_MIGRATION = "\n".join(
    [
        "Operations to perform:",
        "  Apply all migrations: users",
        "Running migrations:",
        "  Applying users.0002_add_email...",
        'CREATE TABLE "users_email" ("id" integer NOT NULL PRIMARY KEY)',
        'ALTER TABLE "users" ADD COLUMN "email" varchar(254)',
        (
            '[django.db.backends] (0.002) SELECT "users"."id" FROM "users" '
            'WHERE "users"."id" = 1; args=(1,)'
        ),
        (
            '[django.db.backends] (0.002) SELECT "users"."id" FROM "users" '
            'WHERE "users"."id" = 2; args=(2,)'
        ),
        (
            '[django.db.backends] (0.002) SELECT "users"."id" FROM "users" '
            'WHERE "users"."id" = 3; args=(3,)'
        ),
        (
            '[django.db.backends] (0.002) SELECT "users"."id" FROM "users" '
            'WHERE "users"."id" = 4; args=(4,)'
        ),
    ]
)


def test_filter_sql_logs_collapses_django_n_plus_one():
    raw = _django_n_plus_one(50)
    compact = filter_sql_logs(raw)

    assert '[django.db.backends] (0.001) SELECT "users"."id"' in compact
    assert "collapsed by usagetrim" in compact
    assert "repeated queries matching" in compact
    assert "IntegrityError: duplicate key value violates unique constraint" in compact
    assert compact.count('WHERE "users"."id" = ') == 1
    before = count_tokens(raw).openai
    after = count_tokens(compact).openai
    assert after < before * 0.15


def test_filter_sql_logs_folds_sqlalchemy_tx_and_pool():
    raw = _sqlalchemy_tx_and_queries(12)
    compact = filter_sql_logs(raw)

    assert "collapsed by usagetrim" in compact
    assert "BEGIN/COMMIT/ROLLBACK/pool checkout lines collapsed" in compact
    assert "FAILED tests/test_models.py::test_create" in compact
    assert "AssertionError: assert 0 == 1" in compact
    assert "checked out from pool" not in compact


def test_filter_sql_logs_prisma_and_drizzle_prefixes():
    prisma = filter_sql_logs(_prisma_query_spam(20))
    assert "prisma:query SELECT" in prisma
    assert "collapsed by usagetrim" in prisma
    assert "PrismaClientKnownRequestError" in prisma
    assert prisma.count("prisma:query SELECT") == 1

    drizzle = filter_sql_logs(_SAMPLE_DRIZZLE_QUERIES)
    assert "Query: select" in drizzle
    assert "collapsed by usagetrim" in drizzle
    assert "Error: Failed query: insert into users" in drizzle


def test_filter_sql_logs_keeps_ddl_and_migrations():
    compact = filter_sql_logs(_SAMPLE_DDL_AND_MIGRATION)

    assert 'CREATE TABLE "users_email"' in compact
    assert 'ALTER TABLE "users" ADD COLUMN "email"' in compact
    assert "Applying users.0002_add_email..." in compact
    assert "collapsed by usagetrim" in compact


def test_filter_sql_logs_leaves_clean_non_sql_untouched():
    clean = "test_auth passed\n2 passed in 0.01s\n"
    assert filter_sql_logs(clean) == clean
    short = "hello\n"
    assert filter_sql_logs(short) == short
    two_queries = "\n".join(
        [
            "SELECT id FROM users WHERE id = 1",
            "SELECT id FROM users WHERE id = 2",
        ]
    )
    # Below the ≥3 collapse threshold and no tx run → identical.
    assert filter_sql_logs(two_queries) == two_queries


def test_filter_sql_logs_idempotent_on_compacted():
    once = filter_sql_logs(_django_n_plus_one(30))
    assert filter_sql_logs(once) == once


def test_auto_specialize_routes_sql_orm_commands_and_density():
    raw = _django_n_plus_one(40)

    for command in (
        "pytest -q",
        "uv run pytest tests/",
        "python manage.py test",
        "npx prisma db seed",
        "alembic upgrade head",
    ):
        routed = auto_specialize_command_output(command, raw)
        assert routed is not None, command
        assert "collapsed by usagetrim" in routed
        assert "IntegrityError" in routed

    # Content-density fallback (e.g. cat of a captured ORM log).
    via_density = auto_specialize_command_output("cat /tmp/orm.log", _prisma_query_spam(15))
    assert via_density is not None
    assert "prisma:query" in via_density
    assert "collapsed by usagetrim" in via_density

    # Non-SQL pytest output stays unspecialized (existing contract).
    assert auto_specialize_command_output("pytest -v", "some output") is None


def test_filter_explain_plan_postgresql_hotspots_and_buffers():
    sample_plan = author_pg_explain_analyze_fixture()
    compact = filter_explain_plan(sample_plan)

    # Hotspots marked
    assert "[HOTSPOT]" in compact
    assert "Seq Scan on orders" in compact
    assert "Hash Join" in compact
    assert "buffer stats folded" in compact

    # Preserves execution time and plan summary
    assert "Execution Time:" in compact
    assert "Planning Time:" in compact

    # Significant token reduction (>65%)
    raw_tokens = count_tokens(sample_plan).openai
    out_tokens = count_tokens(compact).openai
    assert out_tokens < raw_tokens * 0.35, f"Expected >65% reduction, got {out_tokens}/{raw_tokens}"


def test_filter_explain_plan_mysql_full_table_scan():
    sample_mysql = """+----+-------------+-------+------------+------+---------------+------+---------+------+--------+----------+-------------+
| id | select_type | table | partitions | type | possible_keys | key  | key_len | ref  | rows   | filtered | Extra       |
+----+-------------+-------+------------+------+---------------+------+---------+------+--------+----------+-------------+
|  1 | SIMPLE      | users | NULL       | ALL  | NULL          | NULL | NULL    | NULL | 100000 |   100.00 | Using where |
+----+-------------+-------+------------+------+---------------+------+---------+------+--------+----------+-------------+
1 row in set, 1 warning (0.00 sec)"""
    compact = filter_explain_plan(sample_mysql)
    assert "[HOTSPOT: Full Table Scan]" in compact
    assert "users" in compact


def test_filter_explain_plan_idempotent_on_short_or_non_explain():
    short = "SELECT * FROM users WHERE id = 1;\n"
    assert filter_explain_plan(short) == short

    sample_plan = author_pg_explain_analyze_fixture()
    once = filter_explain_plan(sample_plan)
    assert filter_explain_plan(once) == once


def test_auto_specialize_routes_explain_query():
    sample_plan = author_pg_explain_analyze_fixture()
    via_cmd = auto_specialize_command_output(
        "psql -c 'EXPLAIN ANALYZE SELECT * FROM orders'", sample_plan
    )
    assert via_cmd is not None
    assert "[HOTSPOT]" in via_cmd
