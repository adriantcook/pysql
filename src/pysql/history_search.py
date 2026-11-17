"""An fzf-style fuzzy history finder, bound to Ctrl-R.

Replaces readline's ``(reverse-i-search)`` one-liner with a full-screen picker:
type to fuzzy-filter past commands, ↑/↓ to choose, Enter to drop the chosen
command onto the prompt (editable), Esc to cancel. It's a self-contained
prompt_toolkit ``Application`` run *after* the main prompt yields control, so
there's no nested-event-loop trouble.

``fuzzy_match`` and ``rank_history`` are pure functions, unit-tested without a
terminal; ``fuzzy_history_search`` builds and runs the UI (and accepts
``input``/``output`` so it can be driven headlessly in tests).
"""

from __future__ import annotations

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.styles import Style


def fuzzy_match(query: str, text: str) -> int | None:
    """Score ``text`` against ``query`` as a subsequence match (higher is better).

    Returns ``None`` when the query's characters don't appear in order. Adjacent
    matches and matches near word starts score higher, so tighter, earlier hits
    rank first — the behaviour people expect from fzf.
    """
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    score = 0
    pos = last = -1
    for ch in q:
        idx = t.find(ch, pos + 1)
        if idx == -1:
            return None
        if idx == last + 1:
            score += 10  # consecutive run
        if idx == 0 or not t[idx - 1].isalnum():
            score += 5   # start of a word
        score -= idx - (last + 1)  # penalise the gap we skipped
        last = pos = idx
    return score


def rank_history(entries: list[str], query: str) -> list[str]:
    """Most-recent-first, de-duplicated entries that match ``query``, best first."""
    seen: set[str] = set()
    ordered: list[str] = []
    for e in reversed(entries):  # history is oldest→newest
        if e.strip() and e not in seen:
            seen.add(e)
            ordered.append(e)
    scored = []
    for recency, entry in enumerate(ordered):  # recency 0 = newest
        s = fuzzy_match(query, " ".join(entry.split()))
        if s is not None:
            scored.append((s, -recency, entry))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [entry for _, _, entry in scored]


_STYLE = Style.from_dict({
    "prompt": "bold",
    "selected": "reverse",
    "sep": "#555555",
    "status": "reverse",
    "hint": "italic #888888",
})


def fuzzy_history_search(entries, initial: str = "", *, input=None, output=None) -> str | None:
    """Run the picker; return the chosen command, or ``None`` if cancelled."""
    search = Buffer(document=Document(initial, len(initial)), multiline=False)
    state: dict = {"index": 0, "matches": rank_history(entries, initial)}

    def refresh(_=None) -> None:
        state["matches"] = rank_history(entries, search.text)
        state["index"] = 0

    search.on_text_changed += refresh

    def _dims() -> tuple[int, int]:
        size = get_app().output.get_size()
        return max(1, size.rows - 3), max(10, size.columns)

    def result_fragments():
        matches = state["matches"]
        if not matches:
            return [("class:hint", "  no matching history")]
        rows, cols = _dims()
        idx = state["index"]
        start = max(0, min(idx - rows // 2, len(matches) - rows))
        frags = []
        for i in range(start, min(start + rows, len(matches))):
            disp = " ".join(matches[i].split())[: cols - 2]
            if i == idx:
                frags.append(("class:selected", "❯ " + disp + "\n"))
            else:
                frags.append(("", "  " + disp + "\n"))
        return frags

    def status_text():
        n, total = len(state["matches"]), len({e for e in entries if e.strip()})
        return [("class:status",
                 f" {n}/{total}  ·  ↑↓ select · enter edit · esc cancel ")]

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-g")
    @kb.add("escape")
    def _(event):
        event.app.exit(result=None)

    @kb.add("enter")
    def _(event):
        m = state["matches"]
        event.app.exit(result=m[state["index"]] if m else None)

    @kb.add("up")
    @kb.add("c-p")
    def _(event):
        if state["matches"]:
            state["index"] = (state["index"] - 1) % len(state["matches"])

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("c-r")  # pressing Ctrl-R again walks down the list
    def _(event):
        if state["matches"]:
            state["index"] = (state["index"] + 1) % len(state["matches"])

    root = HSplit([
        Window(
            BufferControl(
                buffer=search,
                input_processors=[BeforeInput("  search ❯ ", style="class:prompt")],
            ),
            height=1,
        ),
        Window(height=1, char="─", style="class:sep"),
        Window(FormattedTextControl(result_fragments), wrap_lines=False),
        Window(FormattedTextControl(status_text), height=1),
    ])

    app: Application = Application(
        layout=Layout(root, focused_element=root.children[0]),
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        mouse_support=False,
        input=input,
        output=output,
    )
    return app.run()
