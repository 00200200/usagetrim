"""Deterministic fallback comparison with opt-in Anthropic message references.

Offline: python -m usagetrim.metrics.token_benchmark
Explicit network use: python -m usagetrim.metrics.token_benchmark \
    --fetch-anthropic-reference --anthropic-model MODEL_ID
The latter requires ANTHROPIC_API_KEY and sends only the authored samples below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from usagetrim.metrics.tokenizer import count_model_tokens

SAMPLES = {
    "python": (
        "def authorized(user, scopes):\n"
        "    required = {'read', 'write'}\n"
        "    return user.active and required.issubset(scopes)\n"
    ),
    "json": '{"status":401,"error":"Unauthorized","retry":false,"roles":["reader"]}',
    "markdown": "# Deployment\n\n- Run `uv run pytest`.\n- Preserve `/srv/data`.\n\n**Never** expose secrets.\n",
    "prose": (
        "The request failed because the authentication token expired. "
        "Refresh the credentials before retrying; preserve the original diagnostic log."
    ),
    "whitespace-heavy": "name\t\tvalue\r\n    alpha        42\n\n\n    beta         17  \n",
    "unicode": "Zażółć gęślą jaźń. 認証に失敗しました。 مرحبا بالعالم 🦙🙂\n",
    "mixed": 'HTTP 401 Unauthorized\n```json\n{"message":"JWT wygasł 🦙"}\n```\nRetry with --refresh.\n',
}
# A nonempty baseline avoids invalid empty user messages. Subtracting it accounts
# for message framing, but the delta is contextual, not exact standalone tokens.
REFERENCE_PREFIX = "Text:\n"
ENDPOINT = "https://api.anthropic.com/v1/messages/count_tokens"


class ReferenceCountError(ValueError):
    """A sanitized reference request failure; never contains credentials or bodies."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReferenceCountError("Anthropic reference redirects are refused")


def count_anthropic_message_tokens(text: str, *, model: str, api_key: str | None) -> int:
    """Explicit network operation: count a user message, including API framing.

    This function is never called by runtime token counting or ordinary imports.
    It uses a fixed HTTPS endpoint, refuses redirects and does not log secrets.
    """
    if (
        not isinstance(model, str)
        or not model.startswith("claude-")
        or len(model) <= len("claude-")
        or any(character.isspace() for character in model)
    ):
        raise ReferenceCountError("A concrete Claude model ID is required")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ReferenceCountError("ANTHROPIC_API_KEY is required")
    if any(character in api_key for character in "\r\n"):
        raise ReferenceCountError("Invalid API key")
    if not isinstance(text, str):
        raise ReferenceCountError("Reference text must be a string")
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": text}]},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        ENDPOINT,
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=20) as response:
            payload = response.read(65537)
    except HTTPError as exc:
        raise ReferenceCountError(f"Anthropic count request failed (HTTP {exc.code})") from None
    except (URLError, OSError, ValueError):
        raise ReferenceCountError("Anthropic count request unavailable") from None
    if len(payload) > 65536:
        raise ReferenceCountError("Anthropic count response is too large")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ReferenceCountError("Invalid Anthropic count response") from None
    count = data.get("input_tokens") if isinstance(data, dict) else None
    if type(count) is not int or count < 0:
        raise ReferenceCountError("Invalid Anthropic input_tokens value")
    return count


def benchmark_fallbacks(*, anthropic_model: str | None = None, api_key: str | None = None) -> dict:
    """Compare fixed fallback recipes; references are fetched only on explicit opt-in.

    No regex estimator is calibrated without real reference observations. The
    OpenAI/Gemini-style recipes compared here are not validated against Claude.
    """
    baseline = None
    if anthropic_model is not None:
        baseline = count_anthropic_message_tokens(
            REFERENCE_PREFIX, model=anthropic_model, api_key=api_key
        )
    rows = []
    for name, text in SAMPLES.items():
        base = count_model_tokens(text, "generic").count
        recipes = {
            "utf8-bytes-div4-ceil": base,
            "legacy-utf8-bytes-div4-ceil-x1.10": math.ceil(base * 1.10),
            "legacy-utf8-bytes-div4-ceil-x1.02": math.ceil(base * 1.02),
        }
        row = {
            "sample": name,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "utf8_bytes": len(text.encode("utf-8")),
            "estimates": recipes,
            "reference": None,
            "errors_vs_contextual_increment": None,
        }
        if anthropic_model is not None:
            total = count_anthropic_message_tokens(
                REFERENCE_PREFIX + text, model=anthropic_model, api_key=api_key
            )
            increment = total - baseline
            row["reference"] = {
                "model": anthropic_model,
                "api_message_input_tokens": total,
                "baseline_message_input_tokens": baseline,
                "contextual_increment": increment,
            }
            # Nonpositive deltas cannot support a relative error calculation.
            if increment > 0:
                row["errors_vs_contextual_increment"] = {
                    method: {
                        "absolute_error_tokens": abs(count - increment),
                        "relative_error": abs(count - increment) / increment,
                        "signed_error_tokens": count - increment,
                    }
                    for method, count in recipes.items()
                }
        rows.append(row)
    return {
        "reference_basis": "API user-message increment over a fixed nonempty baseline",
        "baseline_text": REFERENCE_PREFIX,
        "caveat": (
            "API message counts include framing. Baseline subtraction measures a contextual "
            "increment with possible boundary effects, not exact standalone text tokens. "
            "Cross-recipe errors against Claude do not validate other model families."
        ),
        "samples": rows,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-anthropic-reference", action="store_true")
    parser.add_argument("--anthropic-model", help="Concrete Claude model ID; no default")
    args = parser.parse_args(argv)
    if bool(args.anthropic_model) != args.fetch_anthropic_reference:
        parser.error(
            "Reference requests require both --fetch-anthropic-reference and --anthropic-model"
        )
    try:
        report = benchmark_fallbacks(
            anthropic_model=args.anthropic_model,
            api_key=os.environ.get("ANTHROPIC_API_KEY") if args.fetch_anthropic_reference else None,
        )
    except ReferenceCountError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
