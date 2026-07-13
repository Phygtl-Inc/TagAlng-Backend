"""Incremental extraction of `assistant_message` from a streaming JSON completion.

The synthesizer asks the LLM for a JSON object (`{"assistant_message": "...", ...}`),
so raw streamed tokens are JSON — not user-facing text. This extractor is fed the raw
token chunks as they arrive and yields ONLY the decoded content of the
`assistant_message` string value, so the SSE layer can forward it as `delta` frames
while the model is still generating.

Best-effort by design: it never raises on malformed input — it simply stops yielding.
The turn's terminal `result` frame (parsed from the complete response) remains the
authoritative text; deltas are a progressive preview the client replaces with it.
"""

from __future__ import annotations

# Longest partial we must retain while seeking the key across chunk boundaries.
_KEY = '"assistant_message"'

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class AssistantMessageStreamExtractor:
    """Stateful scanner: feed raw completion chunks, get back decoded message text.

    Handles the key and any JSON escape (`\\n`, `\\"`, `\\uXXXX`, surrogate pairs)
    split across chunk boundaries. Caps output at ``max_chars`` to mirror the
    synthesizer's own truncation of the final message.
    """

    def __init__(self, max_chars: int = 1200) -> None:
        self._phase = "seek_key"  # seek_key → seek_colon → seek_quote → in_string → done
        self._window = ""  # rolling buffer while hunting for the key
        self._pending = ""  # incomplete escape sequence held across chunks
        self._emitted = 0
        self._max = max(0, int(max_chars))

    @property
    def done(self) -> bool:
        return self._phase == "done"

    def feed(self, chunk: str) -> str:
        """Consume one raw chunk; return newly decoded assistant_message text ("" if none)."""
        text = str(chunk or "")
        if not text or self._phase == "done":
            return ""
        if self._phase == "seek_key":
            self._window += text
            idx = self._window.find(_KEY)
            if idx < 0:
                # Keep just enough tail to catch a key straddling the next boundary.
                self._window = self._window[-(len(_KEY) - 1) :]
                return ""
            rest = self._window[idx + len(_KEY) :]
            self._window = ""
            self._phase = "seek_colon"
            return self._feed_after_key(rest)
        return self._feed_after_key(text)

    def _feed_after_key(self, text: str) -> str:
        i = 0
        while i < len(text) and self._phase in ("seek_colon", "seek_quote"):
            c = text[i]
            if c in " \t\r\n":
                i += 1
                continue
            expected = ":" if self._phase == "seek_colon" else '"'
            if c != expected:
                # Not the shape we expected (e.g. the match was inside another string).
                # Bail quietly — no deltas is always safe.
                self._phase = "done"
                return ""
            self._phase = "seek_quote" if self._phase == "seek_colon" else "in_string"
            i += 1
        if self._phase != "in_string" or i >= len(text):
            return ""
        return self._decode_string(text[i:])

    def _decode_string(self, chunk: str) -> str:
        s = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == '"':
                self._phase = "done"
                break
            if c != "\\":
                out.append(c)
                i += 1
                continue
            # Escape sequence — may be split across chunks; hold the tail if incomplete.
            if i + 1 >= n:
                self._pending = s[i:]
                break
            e = s[i + 1]
            if e != "u":
                out.append(_SIMPLE_ESCAPES.get(e, e))
                i += 2
                continue
            if i + 6 > n:
                self._pending = s[i:]
                break
            code = _parse_hex(s[i + 2 : i + 6])
            if code is None:
                i += 6  # malformed \u — drop it, keep scanning
                continue
            if 0xD800 <= code <= 0xDBFF:
                # High surrogate: needs the paired \uXXXX before it can be decoded.
                if i + 12 > n:
                    self._pending = s[i:]
                    break
                low = (
                    _parse_hex(s[i + 8 : i + 12])
                    if s[i + 6 : i + 8] == "\\u"
                    else None
                )
                if low is not None and 0xDC00 <= low <= 0xDFFF:
                    combined = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    out.append(chr(combined))
                    i += 12
                    continue
                i += 6  # unpaired high surrogate — drop (never emit invalid UTF-8)
                continue
            if 0xDC00 <= code <= 0xDFFF:
                i += 6  # lone low surrogate — drop
                continue
            out.append(chr(code))
            i += 6
        frag = "".join(out)
        if not frag or self._emitted >= self._max:
            return ""
        frag = frag[: self._max - self._emitted]
        self._emitted += len(frag)
        return frag


def _parse_hex(quad: str) -> int | None:
    if len(quad) != 4:
        return None
    try:
        return int(quad, 16)
    except ValueError:
        return None
