"""Frozen pre-optimization scalar _consume, for differential tests/microbenchmarks.

Only the consumption loop is frozen. Other policy helpers are deliberately
shared with StreamingRedactor; this is not an independent redaction specification.
Never selected by production code.
"""

from usagetrim.core.stream_redactor import CONTROL_LIMIT, LINE_LIMIT, StreamingRedactor


class ScalarRedactor(StreamingRedactor):
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
