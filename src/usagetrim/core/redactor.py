"""High-precision secret redaction for agent outputs.

Stdlib-only regex detectors inspired by Trufflehog / Gitleaks rule shapes.
Matched credentials are replaced with stable type placeholders so redaction is
idempotent and ordinary code, short words, and prior placeholders stay intact.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right

from usagetrim.core.terminal_text import ANSI_SEQUENCE

# Combined detectors. Anthropic (`sk-ant-`) is listed before broader `sk-` forms
# inside the OpenAI group so substitution order stays correct when both fire.
_RULES: tuple[tuple[tuple[str, ...], tuple[tuple[re.Pattern[str], str], ...]], ...] = (
    (
        ("sk-",),
        (
            (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
            (
                re.compile(r"\bsk-(?:proj|admin)-[A-Za-z0-9_\-]{20,}\b"),
                "[REDACTED_OPENAI_KEY]",
            ),
            (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED_OPENAI_KEY]"),
        ),
    ),
    (
        ("AIza",),
        ((re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "[REDACTED_GEMINI_KEY]"),),
    ),
    (
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
        (
            (
                re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
                "[REDACTED_GITHUB_TOKEN]",
            ),
            (
                re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
                "[REDACTED_GITHUB_TOKEN]",
            ),
        ),
    ),
    (
        ("AKIA", "ASIA"),
        ((re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),),
    ),
    (
        ("AQoE", "FwoG"),
        (
            (
                re.compile(r"\b(?:AQoE|FwoG)[A-Za-z0-9/+=]{100,}\b"),
                "[REDACTED_AWS_SESSION]",
            ),
        ),
    ),
    (
        ("xox",),
        ((re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),),
    ),
    (
        ("cfapitoken", "CF_API_TOKEN"),
        (
            (
                re.compile(
                    r"\b(?:cfapitoken|CF_API_TOKEN)[=:\s]+[A-Za-z0-9_\-]{30,}\b",
                    re.IGNORECASE,
                ),
                "[REDACTED_CLOUDFLARE_TOKEN]",
            ),
        ),
    ),
    (
        ("eyJ",),
        (
            (
                re.compile(
                    r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
                ),
                "[REDACTED_JWT]",
            ),
        ),
    ),
    (
        ("Bearer ", "bearer "),
        (
            (
                re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{20,}={0,2}"),
                "Bearer [REDACTED_BEARER]",
            ),
        ),
    ),
    (
        ("PRIVATE KEY",),
        (
            (
                re.compile(
                    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
                    r".*?"
                    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                    re.DOTALL,
                ),
                "[REDACTED_PEM]",
            ),
        ),
    ),
    (
        ("postgres://", "mysql://", "mongodb://", "redis://"),
        (
            (
                re.compile(r"(?:postgres|mysql|mongodb|redis):\/\/[^:\/\s]+:[^@\/\s]+@"),
                "[REDACTED_DB_URL]@",
            ),
        ),
    ),
)

# Flat view for tooling / debugging (pattern, replacement).
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (pattern, replacement) for _, group in _RULES for pattern, replacement in group
]


def redact_secrets(text: str) -> str:
    """Scrub sensitive credentials, tokens, and keys from text before feeding to AI models."""
    if not text:
        return ""
    for needles, group in _RULES:
        if not any(needle in text for needle in needles):
            continue
        for pattern, replacement in group:
            text = pattern.sub(replacement, text)
    if not any(control in text for control in ("\x1b", "\x9b", "\x9d")):
        return text
    # Detect against terminal-visible text, but preserve unrelated ANSI bytes in
    # recoverable originals. Map spans using escape offsets, not a per-char map.
    positions, removed = [], [0]
    for match in ANSI_SEQUENCE.finditer(text):
        positions.append(match.start() - removed[-1])
        removed.append(removed[-1] + match.end() - match.start())
    if not positions:
        return text
    visible = ANSI_SEQUENCE.sub("", text)
    replacements = []
    for needles, group in _RULES:
        if not any(needle in visible for needle in needles):
            continue
        for pattern, replacement in group:
            for match in pattern.finditer(visible):
                start, end = match.span()
                replacements.append(
                    (
                        start + removed[bisect_right(positions, start)],
                        end + removed[bisect_left(positions, end)],
                        replacement,
                    )
                )
    pieces, cursor = [], 0
    for start, end, replacement in sorted(replacements, key=lambda item: (item[0], -item[1])):
        if start < cursor:
            continue
        pieces.extend((text[cursor:start], replacement))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)
