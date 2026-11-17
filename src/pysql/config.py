"""User preferences, read from ``~/.pysqlrc`` at startup.

An INI file with a ``[settings]`` section, e.g.::

    [settings]
    pager            = off
    timing           = on
    vi               = on
    confirm_broadcast = on    ; ask before each \\run-on-active target (\\prompt)
    display_mode     = color  ; or "plain" to switch all colour off
    row_color_1      = #1e1e2e ; alternating zebra-band backgrounds for
    row_color_2      = #313244 ; query-result rows (headers are bold)
    keepalive        = on     ; heartbeat SSM tunnels so idle doesn't reap them
    keepalive_interval = 10   ; seconds between tunnel heartbeats

The on/off settings seed the session and can still be toggled per-session with
``\\pager`` / ``\\timing`` / ``\\v`` / ``\\prompt`` (and persisted with ``\\save``).
``display_mode``, the row colours, and the tunnel ``keepalive`` defaults are read
once at startup (file-only). ``keepalive`` / ``keepalive_interval`` are the global
defaults for SSM tunnels (on, 10s) and a profile's own ``tunnel:`` block overrides
them. Unknown keys and unparseable values are ignored, so a typo never stops
pysql from starting.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

CONFIG_PATH = Path.home() / ".pysqlrc"

# Settings that are booleans, and the words accepted for them.
_BOOL_KEYS = ("pager", "timing", "vi", "confirm_broadcast")
_TRUE = {"on", "true", "yes", "1"}
_FALSE = {"off", "false", "no", "0"}

# A #rgb or #rrggbb colour (with or without the leading '#').
_HEX_RE = re.compile(r"#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Accept British spellings as aliases for the canonical keys/values.
_ROW_COLOR_KEYS = {
    "row_color_1": "row_color_1", "row_colour_1": "row_color_1",
    "row_color_2": "row_color_2", "row_colour_2": "row_color_2",
}


def _norm_hex(value: str) -> str | None:
    """Normalise a hex colour to ``#rrggbb`` (lower-case), or ``None`` if invalid."""
    value = value.strip()
    if not _HEX_RE.match(value):
        return None
    h = value.lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def load_config(path: Path | None = None) -> dict[str, object]:
    """Return recognised settings from the config file (``{}`` if absent/bad).

    Values are ``bool`` for the on/off settings, a ``str`` for ``display_mode``
    (``"plain"`` or ``"color"``) and for the two ``#rrggbb`` row colours.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        return {}
    # interpolation=None: values are literal (a stray '%' must not be special).
    # inline_comment_prefixes=(';',): honour "key = value  ; comment" — but only
    # ';', never '#', since '#' begins a hex colour (e.g. row_color_1 = #1e1e2e).
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";",)
    )
    try:
        parser.read(path)
    except configparser.Error:
        return {}
    if not parser.has_section("settings"):
        return {}

    section = parser["settings"]
    settings: dict[str, object] = {}
    for key in _BOOL_KEYS:
        if key not in section:
            continue
        value = section[key].strip().lower()
        if value in _TRUE:
            settings[key] = True
        elif value in _FALSE:
            settings[key] = False
        # anything else: ignore, keep the built-in default

    # display_mode = plain | color  (also accepts the British "colour")
    if "display_mode" in section:
        value = section["display_mode"].strip().lower()
        if value == "plain":
            settings["display_mode"] = "plain"
        elif value in ("color", "colour"):
            settings["display_mode"] = "color"

    # row_color_1 / row_color_2 (or ..._colour_...): two #hex zebra-band colours.
    for raw_key, canon in _ROW_COLOR_KEYS.items():
        if raw_key in section:
            normalized = _norm_hex(section[raw_key])
            if normalized:
                settings[canon] = normalized

    # keepalive = on|off: global default for SSM tunnel heart-beating.
    if "keepalive" in section:
        value = section["keepalive"].strip().lower()
        if value in _TRUE:
            settings["keepalive"] = True
        elif value in _FALSE:
            settings["keepalive"] = False

    # keepalive_interval = <seconds>: a positive number; anything else is ignored.
    if "keepalive_interval" in section:
        try:
            interval = float(section["keepalive_interval"].strip())
        except ValueError:
            interval = None
        if interval is not None and interval > 0:
            settings["keepalive_interval"] = interval

    return settings


def _word(value: bool) -> str:
    return "on" if value else "off"


# Keys ``\save`` may persist, in output order. Booleans are written on/off; the
# rest (display_mode) are written verbatim. Row colours are deliberately absent:
# nothing toggles them at runtime, so they stay hand-edited in the file.
_SAVE_KEYS = (*_BOOL_KEYS, "display_mode")


def _fmt(value: object) -> str:
    return _word(value) if isinstance(value, bool) else str(value)


def save_config(settings: dict[str, object], path: Path | None = None) -> Path:
    """Write ``settings`` into the ``[settings]`` section, in place.

    Updates existing keys and adds missing ones, but otherwise leaves the file
    untouched — comments and any other sections are preserved. Returns the path.
    """
    path = path or CONFIG_PATH
    lines = path.read_text().splitlines() if path.exists() else []

    start = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == "[settings]"), None
    )
    if start is None:  # no section yet — append one
        block = ["[settings]"] + [f"{k} = {_fmt(settings[k])}" for k in _SAVE_KEYS if k in settings]
        if lines and lines[-1].strip():
            lines.append("")  # blank line before the new section
        lines += block
        path.write_text("\n".join(lines) + "\n")
        return path

    # Section bounds: from the header to the next "[..." header (or EOF).
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].lstrip().startswith("[")),
        len(lines),
    )
    pending = {k: settings[k] for k in _SAVE_KEYS if k in settings}
    for idx in range(start + 1, end):
        body = lines[idx].strip()
        if not body or body.startswith((";", "#")) or "=" not in body:
            continue
        key = body.split("=", 1)[0].strip().lower()
        if key in pending:
            lines[idx] = f"{key} = {_fmt(pending.pop(key))}"
    if pending:  # keys not already present — insert at the end of the section
        lines[end:end] = [f"{k} = {_fmt(v)}" for k, v in pending.items()]
    path.write_text("\n".join(lines) + "\n")
    return path
