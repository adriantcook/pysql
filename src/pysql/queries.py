"""Saved queries for ``\\s``, stored in ``~/.pysql_queries.yaml``.

A flat mapping of ``name -> query template``. A template may contain ``{name}``
placeholders (word characters only) that are filled in interactively each time
the query is run — see :mod:`pysql.binds`.

The file is managed entirely by pysql (``\\s`` saves, ``\\sD`` deletes), so it's
rewritten wholesale rather than edited in place. It isn't a secret store, but
it's kept owner-only since saved queries can embed real filter values.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

QUERIES_PATH = Path.home() / ".pysql_queries.yaml"

# A query name: a single token of letters, digits, '.', '-' or '_' (same shape
# as a profile name), so it never collides with the query text after it.
_NAME_RE = re.compile(r"^[\w.\-]+$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def load_queries(path: Path | None = None) -> dict[str, str]:
    """Parse the saved-queries file into ``{name: query}`` (``{}`` if absent/bad)."""
    path = path or QUERIES_PATH
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        print(f"pysql: could not parse {path} as YAML — no saved queries loaded. ({detail})",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce scalars to str; a value may be int/None if hand-edited.
    return {str(k): str(v) for k, v in data.items() if v is not None}


def save_query(name: str, query: str, path: Path | None = None) -> None:
    """Add or replace a saved query, then rewrite the file. Raises ``OSError``."""
    path = path or QUERIES_PATH
    queries = load_queries(path)
    queries[name] = query
    _dump(queries, path)


def delete_query(name: str, path: Path | None = None) -> bool:
    """Remove a saved query; return ``True`` if it existed. Raises ``OSError``."""
    path = path or QUERIES_PATH
    queries = load_queries(path)
    if name not in queries:
        return False
    del queries[name]
    _dump(queries, path)
    return True


class _Dumper(yaml.SafeDumper):
    """SafeDumper that writes multi-line strings as readable ``|`` block scalars."""


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def _dump(queries: dict[str, str], path: Path) -> None:
    # Sorted for a stable file; width huge so a long one-line query isn't folded.
    text = yaml.dump(
        dict(sorted(queries.items())), Dumper=_Dumper,
        sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000,
    )
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
