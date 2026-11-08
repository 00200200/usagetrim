"""Bounded, deliberately conservative redaction for execution artifacts only.

This is not incremental application of the whole-text regex redactor. A complete
normalized line is quarantined before publication. Credential-looking lines and
PEM bodies are suppressed in their entirety, including malformed credentials.
Overflow or malformed terminal syntax permanently closes the publication gate.
No secret length is assumed. Arbitrary obfuscations/unknown secret types remain
outside the detector's scope, as with the existing pattern-based redactor.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re

LINE_LIMIT = 16 * 1024
CONTROL_LIMIT = 4096
POLICY = "execution-lines-v1"
MASK = "[REDACTED_CREDENTIAL_LINE]"
OMITTED = "[Execution artifact: unsafe or oversized remainder unavailable]\n"
_CREDENTIAL = re.compile(
    r"sk-|AIza|gh[pousr]_|github_pat_|AKIA|ASIA|AQoE|FwoG|IQoJ|xox[baprs]-|"
    r"cfapitoken|cf_api_token|eyJ|bearer|"
    r"aws_(?:access_key_id|secret_access_key|session_token)|"
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://",
    re.IGNORECASE,
)
_PEM_START = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_.-]+")


def _jwt_like(line):
    # One scan of bounded runs avoids a repeated greedy regex search over an
    # arbitrarily long component. Accept JSON-shaped JOSE headers beyond eyJ.
    for match in _TOKEN_RUN.finditer(line):
        run = match.group().strip(".")
        if run.count(".") != 2:
            continue
        header, payload, signature = run.split(".")
        if not header or not payload or not signature:
            continue
        try:
            decoded = base64.b64decode(
                header + "=" * (-len(header) % 4), altchars=b"-_", validate=True
            )
        except (ValueError, binascii.Error):
            continue
        if decoded.lstrip().startswith(b"{"):
            return True
    return False


class StreamingRedactor:
    """Consume bytes, yielding only bounded, sanitized text records.

    LF delimits records; CRLF becomes LF. Bare CR/backspace quarantine the line.
    SGR/OSC can cross input boundaries but are never persisted. UTF-8
    errors use replacement, not surrogateescape. Replacement occurrence remains
    unknown (a literal U+FFFD is indistinguishable in this view).
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._line: list[str] = []
        self._line_bytes = 0
        self._terminal = "text"
        self._control_length = 0
        self._pem = False
        self._pending_value = False
        self._pending_cr = False
        self._tainted_line = False
        self.incomplete = False
        self._closed = False

    def _fail(self):
        self.incomplete = True
        self._line.clear()
        self._line_bytes = 0

    @property
    def has_pending_input(self):
        """Whether a later record can depend on an earlier append/context."""
        return bool(
            self._line
            or self._decoder.getstate()[0]
            or self._terminal != "text"
            or self._pending_cr
            or self._tainted_line
            or self._pem
            or self._pending_value
            or self.incomplete
        )

    def _record(self, newline):
        line = "".join(self._line)
        self._line.clear()
        self._line_bytes = 0
        begin = _PEM_START.search(line) is not None
        end = _PEM_END.search(line) is not None
        credential = _CREDENTIAL.search(line) is not None or _jwt_like(line)
        suppress = self._pem or begin or credential or self._pending_value or self._tainted_line
        self._tainted_line = False
        if begin:
            self._pem = True
        if end:
            self._pem = False
        # A Bearer/credential label may precede its value on another line.
        # Quarantine the next nonempty record too; false positives are intended.
        if credential:
            self._pending_value = True
        elif line.strip():
            self._pending_value = False
        return (MASK if suppress else line) + ("\n" if newline else "")

    def _consume(self, text):
        for character in text:
            if self.incomplete:
                return
            if self._pending_cr:
                self._pending_cr = False
                if character != "\n":
                    self._tainted_line = True
            value = ord(character)
            state = self._terminal
            if state != "text":
                self._control_length += 1
                if self._control_length > CONTROL_LIMIT:
                    self._fail()
                    return
            if state == "escape":
                if character == "[":
                    self._terminal = "csi"
                elif character == "]":
                    self._terminal = "string"
                else:
                    # No terminal emulator: do not silently discard editing,
                    # charset, DCS or other unsupported escape operations.
                    self._fail()
                continue
            if state == "csi":
                if 0x40 <= value <= 0x7E:
                    if character == "m":
                        self._terminal = "text"
                    else:
                        self._fail()
                elif character not in "0123456789;:":
                    self._fail()
                continue
            if state in {"string", "string_escape"}:
                if character in {"\x07", "\x9c"} or (
                    state == "string_escape" and character == "\\"
                ):
                    self._terminal = "text"
                else:
                    self._terminal = "string_escape" if character == "\x1b" else "string"
                continue
            if character == "\x1b":
                self._terminal = "escape"
                self._control_length = 0
            elif character == "\x9b":
                self._terminal = "csi"
                self._control_length = 0
            elif character == "\x9d":
                self._terminal = "string"
                self._control_length = 0
            elif character == "\n":
                yield self._record(True)
            elif character == "\r":
                self._pending_cr = True
            elif character in {"\b", "\x7f"}:
                self._tainted_line = True
            elif character in {"\x00", "\x07"}:
                pass
            elif character == "\t" or value >= 0xA0 or 0x20 <= value < 0x7F:
                self._line_bytes += len(character.encode("utf-8"))
                if self._line_bytes > LINE_LIMIT:
                    self._fail()
                else:
                    self._line.append(character)
            else:
                self._fail()

    def feed(self, data: bytes):
        if self._closed:
            raise ValueError("Redactor is closed")
        if type(data) is not bytes:
            raise TypeError("Execution artifact input must be bytes")
        # The caller owns data; never copy or decode an unbounded append at once.
        for start in range(0, len(data), 4096):
            if self.incomplete:
                break
            yield from self._consume(self._decoder.decode(data[start : start + 4096]))

    def finish(self):
        if self._closed:
            raise ValueError("Redactor is closed")
        self._closed = True
        if not self.incomplete:
            yield from self._consume(self._decoder.decode(b"", final=True))
            if self._terminal != "text":
                self._fail()
            else:
                if self._pending_cr:
                    self._tainted_line = True
                if self._line or self._tainted_line:
                    yield self._record(False)
        if self._pem:
            # A missing PEM terminator cannot establish a full redacted snapshot.
            # All available records are nevertheless safe.
            self.incomplete = True
        if self.incomplete:
            yield OMITTED
        self._line.clear()

    def discard(self):
        self._line.clear()
        self._decoder.reset()
        self._closed = True
