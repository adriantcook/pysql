"""Tab-completion for the pysql prompt.

Context-aware. Backslash commands and their arguments complete from the right
source (meta-command names, then database or table names). In SQL, a small
tokenizer decides whether the cursor wants a *table* (right after FROM / JOIN /
INTO / UPDATE / REFERENCES / TABLE) or a *column* (everywhere else), and scopes
column suggestions to the tables actually named in the statement. A qualified
``alias.`` / ``table.`` completes columns of just that table.

The completer reads live metadata off the session (``databases``, ``tables``,
``table_columns``, ``columns``), refreshed on connect and on ``\\c``. For
multi-line statements it also sees ``pending_text`` — the lines buffered so far
— so table/alias extraction works across the whole statement.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

# (command, help text shown beside the completion)
META_COMMANDS = [
    (r"\d", r"list tables · \d <table> describes one"),
    (r"\d+", r"full table description · \d+ <table>"),
    (r"\dn", "list schemas (current one highlighted)"),
    (r"\dT", r"print the CREATE TABLE statement · \dT <table>"),
    (r"\ds", r"sequences · \ds <name> view (read-only) · \ds <name> <n> set"),
    (r"\dx", "list installed extensions"),
    (r"\l", r"list databases · \l <db> shows stats"),
    (r"\du", r"list roles & groups · \du <role> details one"),
    (r"\dp", r"object-access map · \dp <table|role> details one"),
    (r"\locks", "current table locks, holders, and blockers"),
    (r"\top", r"backend activity · \top <secs> live monitor"),
    (r"\cancel", r"cancel a running query · \cancel <pid>"),
    (r"\kill", r"terminate a backend/user · \kill <pid|user>"),
    (r"\killdb", r"terminate all backends on a database · \killdb <db>"),
    (r"\params", r"cluster parameters · \params <filter> by name"),
    (r"\export", r"export results · \export (select …) [to file] [as csv|tsv|json|jsonl]"),
    (r"\import", r"load a file into a table · \import <file> [to table] [as …]"),
    (r"\cat", r"print a file from disk · \cat <file>"),
    (r"\watch", r"re-run a query on a timer · \watch <query> [interval [count]]"),
    (r"\s", r"saved queries · \s list · \s <name> run · \s <name> <query> save"),
    (r"\sD", r"delete a saved query · \sD <name>"),
    (r"\f", r"run a SQL file · \f <file> [stop]"),
    (r"\file", "run a SQL file from disk"),
    (r"\c", r"connect to a database (\c - = previous)"),
    (r"\C", r"profiles · \C <name> opens one (\C - = previous)"),
    (r"\add", "save the current connection as a profile"),
    (r"\close", "close the current profile session"),
    (r"\w", r"toggle reader/writer endpoint (profiles with a reader: host)"),
    (r"\P", r"pin this session's SSM tunnel to a fixed local port · \P <port> (0 = auto)"),
    (r"\run-on-active", r"run a query on every open session · \run-on-active <query>"),
    (r"\enc", r"encrypt a password for a profile file"),
    (r"\conninfo", "show the current connection"),
    (r"\timing", "toggle query timing"),
    (r"\pager", "result paging on/off"),
    (r"\prompt", r"confirm each \run-on-active target on/off"),
    (r"\color", "colour output on/off"),
    (r"\v", "toggle Vi edit mode"),
    (r"\save", "save current settings to ~/.pysqlrc"),
    (r"\?", "list meta-commands"),
    (r"\q", "quit"),
]

# Single-word keywords only, so multi-word constructs compose naturally as the
# user types token by token rather than clobbering what's already there.
SQL_KEYWORDS = [
    "SELECT", "FROM", "WHERE", "INSERT", "INTO", "UPDATE", "SET", "DELETE",
    "VALUES", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "ON", "USING",
    "GROUP", "ORDER", "BY", "HAVING", "LIMIT", "OFFSET", "DISTINCT", "AS",
    "AND", "OR", "NOT", "NULL", "IS", "IN", "BETWEEN", "LIKE", "ILIKE",
    "EXISTS", "CASE", "WHEN", "THEN", "ELSE", "END", "UNION", "ALL",
    "INTERSECT", "EXCEPT", "WITH", "RETURNING", "CREATE", "TABLE", "INDEX",
    "VIEW", "ALTER", "DROP", "TRUNCATE", "PRIMARY", "KEY", "FOREIGN",
    "REFERENCES", "DEFAULT", "CHECK", "UNIQUE", "COUNT", "SUM", "AVG", "MIN",
    "MAX", "COALESCE", "NULLIF", "CAST", "EXPLAIN", "ANALYZE", "BEGIN",
    "COMMIT", "ROLLBACK", "VACUUM", "ASC", "DESC",
]

_KEYWORDS_UPPER = {k.upper() for k in SQL_KEYWORDS} | {"AS", "OUTER", "NATURAL"}
# Keywords after which an identifier names a table.
_TABLE_CONTEXT = {"FROM", "JOIN", "INTO", "UPDATE", "REFERENCES", "TRUNCATE", "TABLE"}
# Keywords that introduce an extractable table reference (for column scoping).
_TABLE_INTRO = {"FROM", "JOIN", "INTO", "UPDATE"}


# --- tiny SQL tokenizer ---------------------------------------------------

_TOK_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<str>'(?:[^']|'')*'?)          # single-quoted string (maybe unterminated)
    | (?P<dqid>"(?:[^"]|"")*"?)         # double-quoted identifier
    | (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<dot>\.)
    | (?P<comma>,)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# Trailing token under the cursor: optional "qualifier." then the partial word.
_TAIL_RE = re.compile(r'(?:"?(\w+)"?\s*\.\s*)?"?(\w*)"?$')


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Return (kind, text) tokens, dropping whitespace and string literals."""
    tokens: list[tuple[str, str]] = []
    for m in _TOK_RE.finditer(text):
        kind = m.lastgroup
        value = m.group()
        if kind in ("ws", "str"):
            continue
        if kind == "dqid":
            tokens.append(("id", value.strip('"')))
        elif kind == "id":
            tokens.append(("id", value))
        elif kind == "dot":
            tokens.append((".", "."))
        elif kind == "comma":
            tokens.append((",", ","))
        else:
            tokens.append(("op", value))
    return tokens


def _read_name(tokens, i: int):
    """Read a (possibly schema-qualified) name at i; return (bare_name, next_i)."""
    if i >= len(tokens) or tokens[i][0] != "id":
        return None, i
    name = tokens[i][1]
    i += 1
    while i + 1 < len(tokens) and tokens[i][0] == "." and tokens[i + 1][0] == "id":
        name = tokens[i + 1][1]  # keep the last identifier (the table itself)
        i += 2
    return name, i


def _read_alias(tokens, i: int):
    """Read an optional table alias at i; return (alias_or_None, next_i)."""
    if i >= len(tokens) or tokens[i][0] != "id":
        return None, i
    word = tokens[i][1]
    if word.upper() == "AS":
        if i + 1 < len(tokens) and tokens[i + 1][0] == "id":
            return tokens[i + 1][1], i + 2
        return None, i + 1
    if word.upper() in _KEYWORDS_UPPER:
        return None, i
    return word, i + 1


def _extract_tables(tokens) -> tuple[list[str], dict[str, str]]:
    """Find table references; return (ordered unique tables, alias->table map)."""
    referenced: list[str] = []
    alias_map: dict[str, str] = {}
    i, n = 0, len(tokens)
    while i < n:
        kind, text = tokens[i]
        if kind == "id" and text.upper() in _TABLE_INTRO:
            i += 1
            while i < n:  # one or more comma-separated table items
                name, i = _read_name(tokens, i)
                if not name:
                    break
                if name not in referenced:
                    referenced.append(name)
                alias_map[name.lower()] = name
                alias, i = _read_alias(tokens, i)
                if alias:
                    alias_map[alias.lower()] = name
                if i < n and tokens[i][0] == ",":
                    i += 1
                    continue
                break
        else:
            i += 1
    return referenced, alias_map


def _last_keyword(tokens) -> str | None:
    for kind, text in reversed(tokens):
        if kind == "id" and text.upper() in _KEYWORDS_UPPER:
            return text.upper()
    return None


def _prefix_matches(names: Iterable[str], prefix: str, meta: str | None):
    low = prefix.lower()
    start = -len(prefix)
    for name in names:
        if name.lower().startswith(low):
            yield Completion(name, start_position=start, display_meta=meta)


def _profile_names() -> list[str]:
    """Profile section names from the profile file (empty on any error)."""
    try:
        from .profiles import load_profiles

        return sorted(load_profiles())
    except Exception:
        return []


def _saved_query_names() -> list[str]:
    """Saved-query names from the queries file (empty on any error)."""
    try:
        from .queries import load_queries

        return sorted(load_queries())
    except Exception:
        return []


def _trailing_token(text: str) -> str:
    i = len(text)
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    return text[i:]


# Commands whose first argument is a path on the local filesystem.
_PATH_CMDS = {r"\cat", r"\f", r"\file", r"\import"}


def _path_completions(token: str):
    """Complete a filesystem path: files and dirs under the partial's directory.

    An empty ``token`` lists the current directory; ``src/`` lists inside it;
    ``~/`` expands to the home directory. Only the basename is replaced, so the
    typed directory prefix is preserved, and directories get a trailing ``/`` so
    you can keep tabbing into them. Dotfiles stay hidden unless explicitly typed.
    """
    expanded = os.path.expanduser(token)
    dirname, base = os.path.split(expanded)
    list_dir = dirname or "."
    try:
        entries = os.listdir(list_dir)
    except OSError:
        return  # unreadable/nonexistent dir: just offer nothing
    for name in sorted(entries):
        if not name.startswith(base):
            continue
        if base == "" and name.startswith("."):
            continue
        is_dir = os.path.isdir(os.path.join(list_dir, name))
        yield Completion(
            name + ("/" if is_dir else ""),
            start_position=-len(base),
            display_meta="dir" if is_dir else "file",
        )


class SQLCompleter(Completer):
    """Completes meta-commands, their arguments, and SQL identifiers."""

    def __init__(self, session) -> None:
        self.session = session
        self.pending_text = ""  # buffered lines of a multi-line statement

    def get_completions(self, document: Document, complete_event):
        if document.text_before_cursor.lstrip().startswith("\\"):
            yield from self._meta_completions(document.text_before_cursor)
        else:
            yield from self._sql_completions(document)

    def _meta_completions(self, text: str):
        token = _trailing_token(text)
        if token.startswith("\\"):  # still typing the command word itself
            for cmd, meta in META_COMMANDS:
                if cmd.startswith(token):
                    yield Completion(cmd, start_position=-len(token), display_meta=meta)
            return
        cmd = text.split()[0] if text.split() else ""
        if cmd in (r"\c", r"\l"):
            yield from _prefix_matches(self.session.databases, token, "database")
        elif cmd in (r"\d", r"\d+", r"\dT"):
            yield from _prefix_matches(self.session.tables, token, "table")
        elif cmd == r"\du":
            yield from _prefix_matches(self.session.roles, token, "role")
        elif cmd == r"\ds":  # sequence name, but only for the first argument
            ends_space = text[-1:].isspace()
            nargs = len(text.split()) - 1
            arg_no = nargs + 1 if ends_space else nargs
            if arg_no == 1:
                yield from _prefix_matches(self.session.sequences, token, "sequence")
        elif cmd == r"\dp":  # argument may be a table or a role
            yield from _prefix_matches(self.session.tables, token, "table")
            yield from _prefix_matches(self.session.roles, token, "role")
        elif cmd == r"\C":  # profile names (read lazily; only on \C completion)
            yield from _prefix_matches(_profile_names(), token, "profile")
        elif cmd in (r"\s", r"\sD"):  # saved-query name, but only as the first argument
            ends_space = text[-1:].isspace()
            nargs = len(text.split()) - 1
            arg_no = nargs + 1 if ends_space else nargs
            if arg_no == 1:  # after that, \s takes free-form query text
                yield from _prefix_matches(_saved_query_names(), token, "saved query")
        elif cmd in _PATH_CMDS:  # a filesystem path, but only for the first argument
            ends_space = text[-1:].isspace()
            nargs = len(text.split()) - 1
            arg_no = nargs + 1 if ends_space else nargs
            if arg_no == 1:
                yield from _path_completions(token)

    def _sql_completions(self, document: Document):
        text = self.pending_text + document.text_before_cursor
        tail = _TAIL_RE.search(text)
        qualifier, partial = tail.group(1), (tail.group(2) or "")
        referenced, alias_map = _extract_tables(_tokenize(text))

        # Qualified "alias." / "table." -> columns of that one table.
        if qualifier is not None:
            table = alias_map.get(qualifier.lower(), qualifier)
            cols = self.session.table_columns.get(table, [])
            yield from _prefix_matches(cols, partial, f"{table} column")
            return

        last_kw = _last_keyword(_tokenize(text[: tail.start()]))

        # Order matters: with an empty partial every candidate matches, so
        # whatever is yielded first is what Tab selects. Lead with the thing
        # the cursor position actually calls for.
        if last_kw in _TABLE_CONTEXT:
            # Naming a relation (after FROM/JOIN/UPDATE/...): tables only.
            yield from _prefix_matches(self.session.tables, partial, "table")
        elif last_kw is None:
            # Start of a statement: lead with keywords (SELECT, INSERT, ...).
            yield from _prefix_matches(SQL_KEYWORDS, partial, "keyword")
            yield from _prefix_matches(self._scoped_columns(referenced), partial, "column")
        else:
            # Inside a clause: columns first, keywords after.
            yield from _prefix_matches(self._scoped_columns(referenced), partial, "column")
            yield from _prefix_matches(SQL_KEYWORDS, partial, "keyword")

    def _scoped_columns(self, referenced: list[str]) -> list[str]:
        """Columns of the referenced tables, or all columns if none are known."""
        scoped: set[str] = set()
        for table in referenced:
            scoped.update(self.session.table_columns.get(table, []))
        return sorted(scoped) if scoped else self.session.columns
