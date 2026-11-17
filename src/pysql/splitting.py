"""Split accumulated input into complete SQL statements.

Scans character by character, tracking whether the cursor is inside a string,
a quoted identifier, a comment, or a dollar-quoted body, so that a ``;`` only
terminates a statement when it appears in normal state. This lets both
multi-statement lines and multi-line bodies (e.g. a plpgsql function whose
body contains its own semicolons) work correctly.
"""

from __future__ import annotations

import re

# A dollar-quote tag: $$ or $ident$. The identifier can't start with a digit,
# which also keeps parameter placeholders like $1 from matching.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]?\w*\$")


def split_statements(text: str) -> tuple[list[str], str]:
    """Return ``(complete_statements, remainder)``.

    Each complete statement is stripped and has its terminating ``;`` removed.
    The remainder is everything after the last terminator, returned verbatim so
    that newlines and any still-open quote/comment/dollar body survive to be
    continued on the next line.
    """
    statements: list[str] = []
    start = i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "-" and text[i + 1 : i + 2] == "-":            # -- line comment
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif ch == "/" and text[i + 1 : i + 2] == "*":          # /* block comment */
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif ch in ("'", '"'):                                  # '...' or "..."
            i = _skip_quoted(text, i, ch)
        elif ch == "$" and (m := _DOLLAR_TAG.match(text, i)):   # $tag$ ... $tag$
            tag = m.group(0)
            end = text.find(tag, i + len(tag))
            i = n if end < 0 else end + len(tag)
        elif ch == ";":                                         # terminator (normal state)
            stmt = text[start:i].strip()
            if stmt:
                statements.append(stmt)
            i += 1
            start = i
        else:
            i += 1
    return statements, text[start:]


def _skip_quoted(text: str, i: int, quote: str) -> int:
    """Return the index just past a ``'...'`` / ``"..."`` literal starting at i."""
    n = len(text)
    i += 1
    while i < n:
        if text[i] == quote:
            if text[i + 1 : i + 2] == quote:   # doubled quote is an escape
                i += 2
                continue
            return i + 1
        i += 1
    return n  # unterminated: consume the rest, leave the statement incomplete
