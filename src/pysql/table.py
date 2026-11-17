"""Render query results as an aligned Unicode table.

Kept dependency-free and self-contained so it's easy to test and swap out.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# SGR colour escapes are zero-width on screen but not in len(); strip them when
# measuring and padding so coloured cells still line up.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _cell(value: Any) -> str:
    """Render a single value the way a SQL client should.

    NULL is shown explicitly (and distinctly from an empty string) so you can
    tell them apart — one of the small "more information" wins over psql's
    default, which prints nothing for both.
    """
    if value is None:
        return "∅ NULL"
    if isinstance(value, bool):
        return "t" if value else "f"
    # Normalise line endings to "\n": a stray carriage return would otherwise
    # move the cursor to column 0 mid-row and scramble the table. Embedded "\n"s
    # are kept and rendered as stacked lines within the cell (see format_row).
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _display_width(text: str) -> int:
    """Visible width in columns, counting the widest cell line (ANSI-aware).

    Multi-line cells (e.g. text with newlines) are sized by their widest line.
    """
    return max((len(_ANSI_RE.sub("", line)) for line in text.split("\n")), default=0)


def render_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    header_sgr: str | None = None,
    row_bgs: tuple[str, str] | None = None,
) -> str:
    """Return a box-drawn table for the given columns and rows.

    ``header_sgr`` is an SGR parameter string (e.g. ``"1"`` for bold) applied to
    each header cell's text. ``row_bgs`` is a pair of SGR parameter strings
    (e.g. background colours like ``"48;2;30;30;46"``) painted across the whole
    width of each data row, alternating between the two — the classic "zebra"
    banding. Both are ignored when ``None`` (the default), so plain output is
    byte-for-byte unchanged. Width/alignment stay correct because the SGR escapes
    are stripped when measuring (see :func:`_display_width`).
    """
    str_rows = [[_cell(v) for v in row] for row in rows]

    widths = [_display_width(col) for col in columns]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(cell))

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def pad(cell: str, width: int) -> str:  # ljust by *visible* width
        return cell + " " * max(0, width - _display_width(cell))

    def format_row(cells: Sequence[str], *, cell_sgr: str | None = None,
                   bg: str | None = None) -> list[str]:
        # A cell may hold several lines (embedded "\n"); the row is as tall as
        # its tallest cell. Each visual line carries every column's border, so a
        # multi-line value stays boxed in its own column instead of spilling out.
        split = [c.split("\n") for c in cells]
        height = max((len(parts) for parts in split), default=1)
        visual: list[str] = []
        for r in range(height):
            segments = []
            for i, parts in enumerate(split):
                seg = parts[r] if r < len(parts) else ""  # blank-fill short cells
                if cell_sgr:
                    seg = _wrap(seg, cell_sgr)
                segments.append(pad(seg, widths[i]))
            text = "│ " + " │ ".join(segments) + " │"
            # A background wraps each whole line — borders, padding and text — so
            # the band is unbroken edge to edge, on every line of a tall row.
            visual.append(_wrap(text, bg) if bg else text)
        return visual

    out = [line("┌", "┬", "┐")]
    out.extend(format_row(list(columns), cell_sgr=header_sgr))
    out.append(line("├", "┼", "┤"))
    for i, row in enumerate(str_rows):
        bg = row_bgs[i % 2] if row_bgs else None
        out.extend(format_row(row, bg=bg))
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def _wrap(text: str, sgr: str | None) -> str:
    """Wrap ``text`` in an SGR escape (and a reset), or return it unchanged."""
    return f"\x1b[{sgr}m{text}\x1b[0m" if sgr else text
