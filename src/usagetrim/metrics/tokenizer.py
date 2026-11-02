from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import tiktoken

Provider = Literal["claude", "openai", "gemini"]

# Cache tiktoken encodings
_CL100K_ENC = None
_O200K_ENC = None


class ModelFamily(Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    GEMINI = "gemini"
    LLAMA = "llama"
    GENERIC = "generic"


@dataclass(frozen=True)
class ModelTokenCount:
    count: int
    model: str
    family: ModelFamily
    method: str
    is_estimate: bool

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count < 0:
            raise ValueError("count must be a nonnegative integer")


@dataclass(frozen=True)
class CacheBreakpointInfo:
    """Local text threshold comparison, not a guarantee of prompt cache eligibility."""

    model: str
    family: ModelFamily
    token_count: int
    min_tokens: int
    threshold_reached: bool
    tokens_missing: int
    method: str
    is_estimate: bool


def check_cache_breakpoint(
    text: str, model: str = "claude", min_tokens: int = 1024
) -> CacheBreakpointInfo:
    """Compare a local token count with a caller-supplied positive threshold.

    The default threshold is a convenience, not a provider policy. Reaching it
    does not guarantee prompt cache eligibility or a cache hit: actual model
    tokenization, request structure and provider requirements also matter.
    """
    if type(min_tokens) is not int or min_tokens <= 0:
        raise ValueError("min_tokens must be a positive integer (not bool)")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(model, str):
        raise TypeError("model must be a string")

    result = count_model_tokens(text, model)
    return CacheBreakpointInfo(
        model=result.model,
        family=result.family,
        token_count=result.count,
        min_tokens=min_tokens,
        threshold_reached=result.count >= min_tokens,
        tokens_missing=max(0, min_tokens - result.count),
        method=result.method,
        is_estimate=result.is_estimate,
    )


def _utf8_estimate(text: str) -> int:
    """Uncalibrated size heuristic: ceil(UTF-8 bytes / 4), not a tokenizer."""
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        repaired = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        encoded = repaired.encode("utf-8")
    return (len(encoded) + 3) // 4


def _model_encoding(name: str):
    """Reuse only instantiated encodings; never invoke a vocabulary loader.

    Disk-only assets deliberately fall back to an estimate. Reading tiktoken's
    cache safely would require coupling to its private loader and constructors.
    """
    for encoding in (_O200K_ENC, _CL100K_ENC):
        if encoding is not None and getattr(encoding, "name", None) == name:
            return encoding
    from tiktoken import registry

    # Isolated optional integration with tiktoken's in-memory registry. If its
    # representation changes, fail closed rather than calling get_encoding().
    encodings = getattr(registry, "ENCODINGS", None)
    if isinstance(encodings, dict):
        encoding = encodings.get(name)
        if encoding is not None and getattr(encoding, "name", None) == name:
            return encoding
    raise ValueError("No instantiated local tiktoken encoding")


def count_model_tokens(text: str, model: str) -> ModelTokenCount:
    """Count plain text using an explicit family; excludes chat framing/billing.

    Family selection is not selection of a particular provider model version.
    Only o200k_base counts are unestimated encoding counts. Claude, Gemini,
    Llama and generic results never claim to implement those models' tokenizers.
    """
    try:
        family = ModelFamily(model)
    except ValueError:
        supported = ", ".join(item.value for item in ModelFamily)
        raise ValueError(f"Unknown model: {model}. Supported values: {supported}") from None

    if family in {ModelFamily.OPENAI, ModelFamily.CLAUDE, ModelFamily.GEMINI}:
        for name in ("o200k_base", "cl100k_base"):
            try:
                encoding = _model_encoding(name)
                count = len(encoding.encode(text, disallowed_special=()))
                method = encoding.name
                break
            except (OSError, ValueError, RuntimeError, ImportError):
                continue
        else:
            count = _utf8_estimate(text)
            method = "utf8-bytes-div4-ceil"

        if family is ModelFamily.CLAUDE:
            count = math.ceil(count * 1.10)
            method = f"legacy-{method}-x1.10"
        elif family is ModelFamily.GEMINI:
            count = math.ceil(count * 1.02)
            method = f"legacy-{method}-x1.02"
    else:
        count = _utf8_estimate(text)
        method = "utf8-bytes-div4-ceil"
    return ModelTokenCount(
        count=count,
        model=model,
        family=family,
        method=method,
        is_estimate=family is not ModelFamily.OPENAI or method != "o200k_base",
    )


def get_cl100k():
    global _CL100K_ENC
    if _CL100K_ENC is None:
        _CL100K_ENC = tiktoken.get_encoding("cl100k_base")
    return _CL100K_ENC


def get_o200k():
    global _O200K_ENC
    if _O200K_ENC is None:
        try:
            _O200K_ENC = tiktoken.get_encoding("o200k_base")
        except Exception:
            _O200K_ENC = get_cl100k()
    return _O200K_ENC


@dataclass(frozen=True)
class TokenCount:
    claude: int
    openai: int
    gemini: int

    @property
    def avg(self) -> int:
        return (self.claude + self.openai + self.gemini) // 3


@dataclass(frozen=True)
class TokenReductionMetrics:
    raw_tokens: TokenCount
    compact_tokens: TokenCount
    raw_chars: int
    compact_chars: int
    raw_lines: int
    compact_lines: int

    @property
    def saved_tokens(self) -> TokenCount:
        return TokenCount(
            claude=max(0, self.raw_tokens.claude - self.compact_tokens.claude),
            openai=max(0, self.raw_tokens.openai - self.compact_tokens.openai),
            gemini=max(0, self.raw_tokens.gemini - self.compact_tokens.gemini),
        )

    @property
    def reduction_pct(self) -> float:
        if self.raw_tokens.avg == 0:
            return 0.0
        saved = self.raw_tokens.avg - self.compact_tokens.avg
        return round((saved / self.raw_tokens.avg) * 100.0, 1)

    @property
    def char_reduction_pct(self) -> float:
        if self.raw_chars == 0:
            return 0.0
        return round(((self.raw_chars - self.compact_chars) / self.raw_chars) * 100.0, 1)


def count_tokens(text: str) -> TokenCount:
    """Return local text-size estimates, not provider billing or quota usage.

    OpenAI uses o200k_base (cl100k_base fallback), not a model-specific tokenizer.
    Claude and Gemini are uncalibrated multipliers retained for compatibility.
    These estimates do not establish counts for Astra, Fable, Opus, or other models.
    """
    if not text:
        return TokenCount(claude=0, openai=0, gemini=0)

    try:
        openai_count = len(get_o200k().encode(text, disallowed_special=()))
    except Exception:
        openai_count = len(get_cl100k().encode(text, disallowed_special=()))

    # Claude token estimation
    # Legacy heuristic; not a measured Anthropic tokenizer.
    claude_count = int(math.ceil(openai_count * 1.10))

    # Gemini SentencePiece estimation
    gemini_count = int(math.ceil(openai_count * 1.02))

    return TokenCount(
        claude=claude_count,
        openai=openai_count,
        gemini=gemini_count,
    )


def compute_metrics(raw_text: str, compact_text: str) -> TokenReductionMetrics:
    raw_tok = count_tokens(raw_text)
    compact_tok = count_tokens(compact_text)

    raw_lines = len(raw_text.splitlines()) if raw_text else 0
    compact_lines = len(compact_text.splitlines()) if compact_text else 0

    return TokenReductionMetrics(
        raw_tokens=raw_tok,
        compact_tokens=compact_tok,
        raw_chars=len(raw_text),
        compact_chars=len(compact_text),
        raw_lines=raw_lines,
        compact_lines=compact_lines,
    )
