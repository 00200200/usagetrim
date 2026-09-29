from usagetrim.core.cleaner import (
    CleanerOptions,
    CodeCleanerOptions,
    clean_source_code,
    collapse_blank_lines,
    compact_terminal_output,
    deduplicate_repetitive_lines,
    fold_banner_docstrings,
    fold_commented_out_code,
    fold_license_header,
    resolve_carriage_returns,
    strip_ansi,
)
from usagetrim.core.skeleton import strip_comments_and_blanks

APACHE_BANNER = """\
# Copyright 2024 Example Corp
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# SPDX-License-Identifier: Apache-2.0

def greet(name: str) -> str:
    return f"hi {name}"
"""

SPDX_TS = """\
// SPDX-License-Identifier: MIT
// Copyright (c) 2024 Example Corp
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.

export function add(a: number, b: number): number {
  return a + b;
}
"""

COMMENTED_OUT_BLOCK = """\
def live() -> int:
    return 1

# def old_helper(x):
#     y = x * 2
#     if y > 10:
#         return y
#     for i in range(y):
#         print(i)
#     return None
# class Legacy:
#     def run(self):
#         return self

def more() -> int:
    return 2
"""

BLANK_HEAVY = """\
def a():
    return 1



def b():
    return 2
"""

BANNER_DOCSTRING = '''\
"""
Copyright (c) 2024 Example Corp. All rights reserved.
Licensed under the Apache License, Version 2.0.
SPDX-License-Identifier: Apache-2.0
"""

def real() -> str:
    """Return a helpful greeting for the caller."""
    return "ok"
'''


def test_strip_ansi():
    ansi_str = "\x1b[31;1mERROR:\x1b[0m Failed with \x1b[32mstatus 500\x1b[0m\x1b[?25h"
    cleaned = strip_ansi(ansi_str)
    assert cleaned == "ERROR: Failed with status 500"


def test_resolve_carriage_returns():
    progress = "Downloading... 10%\rDownloading... 50%\rDownloading... 100%\nDone!"
    resolved = resolve_carriage_returns(progress)
    assert "Downloading... 10%" not in resolved
    assert "Downloading... 100%" in resolved
    assert "Done!" in resolved


def test_deduplicate_repetitive_lines():
    lines = [
        "Building chunk 1",
        "Warning: unused var",
        "Warning: unused var",
        "Warning: unused var",
        "Warning: unused var",
        "Done chunk 1",
    ]
    res = deduplicate_repetitive_lines(lines, max_consecutive=2)
    assert len(res) < len(lines)
    assert any("identical lines omitted" in line for line in res)


def test_compact_terminal_output_preserves_error():
    # Build 100 lines of noise followed by a Python traceback
    routine = [f"Step {i}: processing item {i}..." for i in range(1, 101)]
    error = [
        "Traceback (most recent call last):",
        '  File "main.py", line 42, in calculate',
        "    return 100 / divisor",
        "ZeroDivisionError: division by zero",
    ]
    full_output = "\n".join(routine + error)

    opts = CleanerOptions(max_lines=30, head_lines=10, tail_lines=20, preserve_errors=True)
    compacted = compact_terminal_output(full_output, opts)

    # Must preserve first steps
    assert "Step 1: processing item 1..." in compacted
    # Must preserve the exact error
    assert "Traceback (most recent call last):" in compacted
    assert "ZeroDivisionError: division by zero" in compacted
    # Must omit middle routine steps
    assert "lines of routine output omitted by usagetrim" in compacted


def test_compact_terminal_output_no_error():
    lines = [f"Item {i}" for i in range(1, 150)]
    full_output = "\n".join(lines)
    opts = CleanerOptions(max_lines=30, head_lines=10, tail_lines=10, preserve_errors=True)
    compacted = compact_terminal_output(full_output, opts)

    assert "Item 1" in compacted
    assert "Item 149" in compacted
    assert "lines omitted by usagetrim" in compacted


def test_fold_license_header_python_apache():
    folded = fold_license_header(APACHE_BANNER, suffix=".py")
    assert "SPDX-License-Identifier: Apache-2.0" not in folded
    assert "http://www.apache.org/licenses/LICENSE-2.0" not in folded
    assert "# [License: Apache-2.0 (" in folded
    assert "lines folded)]" in folded
    assert "def greet(name: str) -> str:" in folded
    assert 'return f"hi {name}"' in folded


def test_fold_license_header_typescript_mit():
    folded = fold_license_header(SPDX_TS, suffix=".ts")
    assert "Permission is hereby granted" not in folded
    assert "// [License: MIT (" in folded
    assert "export function add(a: number, b: number): number {" in folded
    assert "return a + b;" in folded


def test_fold_license_skips_short_non_license_comments():
    src = "# TODO: refine API\n# also note X\ndef f():\n    return 1\n"
    assert fold_license_header(src, suffix=".py") == src


def test_fold_commented_out_code_block():
    folded = fold_commented_out_code(COMMENTED_OUT_BLOCK, suffix=".py")
    assert "def old_helper" not in folded
    assert "class Legacy" not in folded
    assert "commented-out code folded by usagetrim" in folded
    assert "def live() -> int:" in folded
    assert "def more() -> int:" in folded
    assert "return 1" in folded
    assert "return 2" in folded


def test_collapse_blank_lines():
    collapsed = collapse_blank_lines(BLANK_HEAVY, max_run=1)
    assert "\n\n\n" not in collapsed
    assert "def a():" in collapsed
    assert "def b():" in collapsed
    # Exactly one blank between functions
    assert "return 1\n\ndef b():" in collapsed


def test_fold_banner_docstring_keeps_real_description():
    folded = fold_banner_docstrings(BANNER_DOCSTRING, suffix=".py")
    assert "SPDX-License-Identifier: Apache-2.0" not in folded
    assert "[License: Apache-2.0" in folded
    assert '"""Return a helpful greeting for the caller."""' in folded
    assert "def real() -> str:" in folded
    assert 'return "ok"' in folded


def test_clean_source_code_preserves_executable_semantics():
    cleaned = clean_source_code(APACHE_BANNER, suffix=".py")
    assert "def greet(name: str) -> str:" in cleaned
    assert 'return f"hi {name}"' in cleaned
    assert "# [License: Apache-2.0 (" in cleaned

    # Descriptive docstring must survive even with stripping path
    sample = '''\
"""Useful module that wires auth tokens for the gateway."""

def ping() -> str:
    """Health check used by the load balancer probe."""
    # note: keep this
    return "pong"
'''
    cleaned = clean_source_code(
        sample,
        suffix=".py",
        options=CodeCleanerOptions(strip_remaining_comments=True),
    )
    assert "Useful module that wires auth tokens" in cleaned
    assert "Health check used by the load balancer probe" in cleaned
    assert "def ping() -> str:" in cleaned
    assert 'return "pong"' in cleaned
    assert "# note: keep this" not in cleaned


def test_strip_comments_and_blanks_uses_semantic_folds():
    result = strip_comments_and_blanks(APACHE_BANNER, suffix=".py")
    assert "# [License: Apache-2.0 (" in result
    assert "def greet(name: str) -> str:" in result
    assert 'return f"hi {name}"' in result
    # Ordinary prose comments elsewhere still go away via strip path
    with_note = APACHE_BANNER + "\n# leftover note\n"
    stripped = strip_comments_and_blanks(with_note, suffix=".py")
    assert "leftover note" not in stripped
