"""Human-friendly EXPLAIN / EXPLAIN ANALYZE rendering.

``psql`` prints the planner's raw text, which is dense and reads bottom-up. We
instead run the plan as JSON, compute each node's *self* cost/time (its own
share, with children subtracted — the flame-graph signal), and draw the plan
tree with proportional bars so the expensive nodes jump out. Alongside each
node we surface the facts a human actually acts on: row-estimate misses (stale
stats), loop counts, and blocks read from disk.

Two entry points:

* :func:`rewrite_explain` — if a statement is an ``EXPLAIN`` we can handle,
  returns the rewritten ``EXPLAIN (..., FORMAT JSON) <query>`` plus whether it
  was an ANALYZE (so the caller runs *that* instead, never the original twice).
  Returns ``None`` for non-EXPLAIN statements and for an EXPLAIN that pins an
  explicit non-JSON ``FORMAT`` (we leave those to the normal table renderer).
* :func:`render_explain` — turn the decoded JSON plan into the display block.
"""

from __future__ import annotations

import re
from typing import Any

_EXPLAIN_RE = re.compile(r"^\s*EXPLAIN\b", re.IGNORECASE)
# Options that are legal *without* parentheses (the legacy EXPLAIN syntax).
_LEGACY_OPTS = {"ANALYZE", "ANALYSE", "VERBOSE"}
_FALSY = {"FALSE", "OFF", "0", "F", "NO"}


def rewrite_explain(sql: str) -> tuple[str, bool] | None:
    """Return ``(json_explain_sql, is_analyze)`` for an EXPLAIN we can pretty-print.

    ``None`` means "not ours": either not an EXPLAIN, or one that explicitly
    asks for a non-JSON output format (which the caller should run as-is).
    """
    m = _EXPLAIN_RE.match(sql)
    if not m:
        return None

    rest = sql[m.end() :].lstrip()
    opts: list[tuple[str, str]] = []  # (NAME_UPPER, raw option text)

    if rest.startswith("("):
        close = rest.find(")")
        if close == -1:
            return None  # unbalanced; let Postgres report the syntax error
        query = rest[close + 1 :]
        for part in rest[1:close].split(","):
            part = part.strip()
            if part:
                opts.append((part.split()[0].upper(), part))
    else:
        # Legacy form: a run of bare ANALYZE / ANALYSE / VERBOSE keywords.
        query = rest
        while (mm := re.match(r"\s*([A-Za-z]+)\b", query)) and mm.group(1).upper() in _LEGACY_OPTS:
            word = mm.group(1).upper()
            opts.append(("ANALYZE" if word in ("ANALYZE", "ANALYSE") else word, word))
            query = query[mm.end() :]

    analyze = False
    user_format: str | None = None
    kept: list[str] = []
    for name, raw in opts:
        if name in ("ANALYZE", "ANALYSE"):
            analyze = raw.split()[-1].upper() not in _FALSY if " " in raw else True
            kept.append(raw)
        elif name == "FORMAT":
            bits = raw.split()
            user_format = bits[1].upper() if len(bits) > 1 else None
        else:
            kept.append(raw)

    if user_format and user_format != "JSON":
        return None  # user wants text/yaml/xml — respect it, don't intercept

    kept.append("FORMAT JSON")
    return f"EXPLAIN ({', '.join(kept)}) {query.strip()}", analyze


# --- rendering ------------------------------------------------------------

_BLOCKS = " ▏▎▍▌▋▊▉█"  # 0..8 eighths of a cell, for sub-character bar precision
_BAR_W = 14

# Bar colour flags the *access method*: indexed reads are good (green), a
# sequential scan is the "could this use an index?" flag (red). Everything else
# (joins, sorts, aggregates…) is coloured by heat instead — see below.
_INDEXED_SCANS = {
    "Index Scan",
    "Index Only Scan",
    "Bitmap Index Scan",
    "Bitmap Heap Scan",
    "Tid Scan",
}


def _fmt_int(n: float) -> str:
    return f"{int(round(n)):,}"


def _fmt_ms(ms: float) -> str:
    if ms >= 100:
        return f"{ms:.0f} ms"
    if ms >= 10:
        return f"{ms:.1f} ms"
    return f"{ms:.3f} ms"


def _bar(frac: float) -> str:
    frac = max(0.0, min(1.0, frac))
    eighths = round(frac * _BAR_W * 8)
    full, rem = divmod(eighths, 8)
    bar = "█" * full + (_BLOCKS[rem] if rem else "")
    return bar.ljust(_BAR_W)


def render_explain(data: Any, analyze: bool, color: bool) -> str:
    """Render decoded EXPLAIN JSON (``[{"Plan": ...}]``) as a readable tree."""

    def c(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    dim = lambda t: c(t, "2")  # noqa: E731
    root = data[0] if isinstance(data, list) else data
    plan = root["Plan"]

    def metric(node: dict) -> float:
        if analyze:
            return node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1)
        return node.get("Total Cost", 0.0)

    def self_metric(node: dict) -> float:
        kids = sum(metric(ch) for ch in node.get("Plans", []))
        return max(0.0, metric(node) - kids)

    total = metric(plan) or 1.0
    unit = "time" if analyze else "cost"

    # Flatten the tree into display rows, carrying each node's branch prefix.
    rows: list[tuple[str, dict]] = []

    def walk(node: dict, pad: str, connector: str, is_last: bool) -> None:
        rows.append((pad + connector, node))
        child_pad = pad + ("" if not connector else ("   " if is_last else "│  "))
        kids = node.get("Plans", [])
        for i, ch in enumerate(kids):
            last = i == len(kids) - 1
            walk(ch, child_pad, "└─ " if last else "├─ ", last)

    walk(plan, "", "", True)

    hottest = max(rows, key=lambda r: self_metric(r[1]))[1]
    misestimates = 0
    seen_seq_scan = False
    lines: list[str] = []

    # Header.
    mode = "EXPLAIN ANALYZE" if analyze else "EXPLAIN"
    if analyze:
        sub = (
            f"execution {_fmt_ms(root.get('Execution Time', 0.0))}"
            f" · planning {_fmt_ms(root.get('Planning Time', 0.0))}"
            f" · bars show self time"
        )
    else:
        sub = "estimated plan · bars show self cost · add ANALYSE for real timings"
    lines.append(c(mode, "1;32") + "  " + dim(sub))
    lines.append("")

    for pad, node in rows:
        frac = self_metric(node) / total
        ntype = node["Node Type"]
        if ntype == "Seq Scan":
            shade = "31"  # sequential scan — flag it
            seen_seq_scan = True
        elif ntype in _INDEXED_SCANS:
            shade = "32"  # indexed access — good
        else:  # structural node: fall back to heat (how much time it owns)
            shade = "31" if frac >= 0.5 else "33" if frac >= 0.2 else "36" if frac >= 0.02 else "2"
        gutter = c(_bar(frac), shade) + " " + f"{frac * 100:3.0f}%" + "  "

        # Node label: type (+ join flavour) + the object it touches.
        label = node["Node Type"]
        jt = node.get("Join Type")
        if jt and jt != "Inner":
            label += f" {jt}"
        rel, idx = node.get("Relation Name"), node.get("Index Name")
        obj = node.get("CTE Name") or node.get("Function Name")
        if idx and rel:
            label += f" using {idx} on {rel}"
        elif rel:
            label += f" on {rel}"
        elif obj:
            label += f" on {obj}"
        alias = node.get("Alias")
        if alias and rel and alias != rel:
            label += dim(f" ({alias})")

        # Dim stats tail.
        parts: list[str] = []
        if analyze:
            incl = node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1)
            parts.append(_fmt_ms(self_metric(node)) + " self")
            if abs(incl - self_metric(node)) > 0.0005:
                parts.append(_fmt_ms(incl) + " incl")
            loops = node.get("Actual Loops", 1)
            arows = node.get("Actual Rows", 0)
            prows = node.get("Plan Rows", 0)
            rowstat = f"{_fmt_int(arows * loops)} rows"
            if prows:
                hi, lo = max(prows, arows), max(1, min(prows, arows))
                # Only a *large* miss matters: a 20-vs-0 on a tiny table is noise.
                if hi >= 100 and hi / lo >= 10:
                    misestimates += 1
                    rowstat += c(f" ✗est {_fmt_int(prows)}", "33")
                elif abs(prows - arows) > 0:
                    rowstat += dim(f" (est {_fmt_int(prows)})")
            parts.append(rowstat)
            if loops > 1:
                parts.append(f"{_fmt_int(loops)} loops")
            read = node.get("Shared Read Blocks", 0) + node.get("Temp Read Blocks", 0)
            if read:
                parts.append(c(f"{_fmt_int(read)} blk read", "35"))
        else:
            parts.append(
                f"cost {node.get('Startup Cost', 0.0):.2f}..{node.get('Total Cost', 0.0):.2f}"
            )
            parts.append(f"est {_fmt_int(node.get('Plan Rows', 0))} rows")

        lines.append(gutter + pad + label + "  " + dim("· ".join(p + " " for p in parts).rstrip()))

    # Footer.
    lines.append("")
    hot_label = hottest["Node Type"]
    if hottest.get("Relation Name"):
        hot_label += f" on {hottest['Relation Name']}"
    hot_pct = self_metric(hottest) / total * 100
    lines.append(dim("hottest: ") + hot_label + dim(f"  {hot_pct:.0f}% of {unit}"))
    if seen_seq_scan:
        lines.append(
            dim("bar colour: ") + c("sequential scan", "31") + dim(" (an index may help) · ")
            + c("index access", "32") + dim(" · other nodes by heat")
        )
    if misestimates:
        lines.append(
            c("⚠ ", "33")
            + dim(f"{misestimates} node(s) off by ≥10× on row estimates — try ANALYZE on the table")
        )
    if (root.get("Planning Time") and analyze and root.get("Execution Time")
            and root["Planning Time"] > root["Execution Time"]):
        lines.append(dim("note: planning took longer than execution"))

    return "\n".join(lines)
