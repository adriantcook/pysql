"""Interactive bind-value entry for saved queries with ``{placeholder}``s.

A saved query may embed placeholders like ``where id = '{1}'`` or
``created_at > '{since}'``. When it's run, :func:`prompt_for_binds` pops up a
small form — one field per distinct placeholder — where **Tab**/**Shift-Tab**
(or ↓/↑) move between fields, **Enter** submits and **Esc** cancels. The entered
text is substituted **literally** for each ``{name}`` (so the template controls
quoting, e.g. ``'{1}'`` keeps the quotes around the value).

``extract_placeholders`` and ``substitute`` are pure and unit-testable without a
terminal; ``prompt_for_binds`` builds and runs the UI (and accepts
``input``/``output`` so it can be driven headlessly in tests).
"""

from __future__ import annotations

import re

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

# A placeholder is {word_chars}: letters, digits, underscore. Requiring \w+
# (not .+) avoids swallowing Postgres literals like '{1,2,3}'::int[] or JSON
# '{"a":1}', which contain commas/quotes/colons and so never match.
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def extract_placeholders(text: str) -> list[str]:
    """The distinct ``{name}`` placeholders in ``text``, first-appearance order."""
    seen: set[str] = set()
    order: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            order.append(name)
    return order


def substitute(text: str, values: dict[str, str]) -> str:
    """Replace each ``{name}`` with ``values[name]`` (literally); unknowns kept."""
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


_STYLE = Style.from_dict({
    "title": "bold",
    "query": "#888888",       # the query template, shown for context
    "bind": "bold #00afff",   # the {placeholders} inside it, highlighted
    "label": "bold #00afff",  # the field label (matches its placeholder)
    "hint": "italic #888888",
    "sep": "#555555",
})


def _query_fragments(query: str):
    """The query as styled fragments, with each ``{placeholder}`` highlighted."""
    frags, last = [], 0
    for m in _PLACEHOLDER_RE.finditer(query):
        if m.start() > last:
            frags.append(("class:query", query[last:m.start()]))
        frags.append(("class:bind", m.group(0)))
        last = m.end()
    if last < len(query):
        frags.append(("class:query", query[last:]))
    return frags


def prompt_for_binds(
    names: list[str], *, title: str = "", query: str = "", input=None, output=None
) -> dict[str, str] | None:
    """Prompt for a value per name; return ``{name: value}`` or ``None`` if cancelled.

    Fields are shown in ``names`` order. ``query`` (when given) is displayed above
    them with its ``{placeholders}`` highlighted, so you can see what each field
    fills in. Tab / Shift-Tab (and ↓ / ↑) move between fields; Enter accepts all
    (the caller stages the filled query on the prompt); Esc or Ctrl-C cancels.
    """
    if not names:
        return {}

    buffers = {name: Buffer(multiline=False) for name in names}
    label_width = max(len(n) for n in names)

    def field_row(name: str) -> VSplit:
        label = Window(
            FormattedTextControl([("class:label", name.rjust(label_width))]),
            width=label_width, height=1, dont_extend_width=True,
        )
        entry = Window(BufferControl(buffer=buffers[name]), height=1)
        return VSplit([Window(width=2, char=" "), label, Window(width=2, char=" "), entry])

    header = title or "Fill in the query binds"
    rows = [Window(FormattedTextControl([("class:title", f"  {header}")]), height=1)]
    if query:
        # The full query for context, wrapped; placeholders highlighted so you can
        # see where each field lands even with positional binds like {1}, {2}.
        rows.append(Window(height=1))  # spacer
        rows.append(Window(FormattedTextControl([("", "  "), *_query_fragments(query)]),
                           wrap_lines=True))
    rows += [
        Window(FormattedTextControl(
            [("class:hint", "  Tab/⇧Tab or ↓/↑ move · Enter → paste to prompt · Esc cancel")]),
            height=1),
        Window(height=1, char="─", style="class:sep"),
        Window(height=1),  # spacer
        HSplit([field_row(n) for n in names]),
    ]
    root = HSplit(rows)

    kb = KeyBindings()

    @kb.add("tab", eager=True)
    @kb.add("down", eager=True)
    def _(event):
        event.app.layout.focus_next()

    @kb.add("s-tab", eager=True)
    @kb.add("up", eager=True)
    def _(event):
        event.app.layout.focus_previous()

    @kb.add("enter", eager=True)
    def _(event):
        event.app.exit(result={n: b.text for n, b in buffers.items()})

    @kb.add("c-c", eager=True)
    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result=None)

    first_entry = root.children[-1].children[0].children[-1]  # first field's entry Window
    app: Application = Application(
        layout=Layout(root, focused_element=first_entry),
        key_bindings=kb,
        style=_STYLE,
        full_screen=False,
        erase_when_done=True,  # clear the form on exit; the caller echoes the result
        mouse_support=False,
        input=input,
        output=output,
    )
    return app.run()
