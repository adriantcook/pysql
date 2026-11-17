"""pysql — an interactive PostgreSQL prompt.

A deliberately small REPL to build on. It connects, reads statements until a
semicolon, runs them, and prints results with a bit more context than psql
gives you by default (timing, clearer NULLs, richer error diagnostics).
"""

from __future__ import annotations

import csv as _csv
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

import psycopg
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import vi_navigation_mode
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from psycopg import sql
from psycopg.types.json import Json

from . import __version__, crypto, rds
from .binds import extract_placeholders, prompt_for_binds, substitute
from .completion import SQLCompleter
from .config import CONFIG_PATH, load_config, save_config
from .ddl import build_create_table
from .explain import render_explain, rewrite_explain
from .history_search import fuzzy_history_search
from .privileges import (
    TABLE_PRIVS,
    render_privilege_overview,
    render_role_privileges,
    render_table_privileges,
)
from .profiles import (
    Profile,
    active_profile_path,
    load_profiles,
    profile_conninfo,
    save_profile,
    set_profile_password,
)
from .queries import (
    QUERIES_PATH,
    delete_query,
    load_queries,
    save_query,
    valid_name,
)
from .roles import render_role_detail, render_roles
from .splitting import split_statements
from .table import render_table
from .tunnel import TunnelError, set_keepalive_defaults, tunnel_from_profile

HISTORY_FILE = Path.home() / ".pysql_history"

# ANSI styling, kept tiny. Falls back to no color when stdout isn't a terminal,
# unless ~/.pysqlrc's display_mode overrides it (set in repl()); \color toggles
# it at runtime.
_COLOR = sys.stdout.isatty()

# The two zebra-band hex colours from ~/.pysqlrc (kept regardless of whether
# colour is currently on), and the SGR codes derived from them when it is on.
_ROW_COLORS: tuple[str, str] | None = None
_ROW_BGS: tuple[str, str] | None = None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _hex_to_bg_sgr(hex_color: str) -> str:
    """``#rrggbb`` → an SGR truecolor *background* parameter, e.g. ``48;2;30;30;46``."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"48;2;{r};{g};{b}"


def _refresh_row_bgs() -> None:
    """Recompute the zebra-band SGR codes from the current colour state.

    Banding needs both row colours *and* colour to be on; otherwise it's off.
    """
    global _ROW_BGS
    if _COLOR and _ROW_COLORS:
        _ROW_BGS = (_hex_to_bg_sgr(_ROW_COLORS[0]), _hex_to_bg_sgr(_ROW_COLORS[1]))
    else:
        _ROW_BGS = None


def _set_color(on: bool) -> None:
    """Turn colour output on/off (and refresh the banding to match)."""
    global _COLOR
    _COLOR = on
    _refresh_row_bgs()


def _apply_display_settings(settings: dict) -> None:
    """Apply ``display_mode`` and the row colours from config to module globals.

    ``display_mode`` forces colour on/off (otherwise it follows ``isatty``); the
    row colours are remembered so ``\\color`` can restore banding after a toggle.
    """
    global _ROW_COLORS
    c1, c2 = settings.get("row_color_1"), settings.get("row_color_2")
    _ROW_COLORS = (c1, c2) if (c1 and c2) else None
    mode = settings.get("display_mode")
    if mode == "plain":
        _set_color(False)
    elif mode == "color":
        _set_color(True)
    else:
        _refresh_row_bgs()  # colour follows the isatty default; bands still apply


def _dim(text: str) -> str:
    return _c(text, "2")


def _bold(text: str) -> str:
    return _c(text, "1")


def _header_sgr() -> str | None:
    """SGR for a table's column headers — bold when colour is on, else nothing."""
    return "1" if _COLOR else None


def _red(text: str) -> str:
    return _c(text, "31")


def _green(text: str) -> str:
    return _c(text, "32")


def _banner(session) -> str:
    """A compact, multi-line connection summary shown at startup.

    When the session is reached through a tunnel, the local 127.0.0.1 endpoint is
    uninteresting — show the *real* database host it forwards to, with a marker.
    """
    info = session.conn.info
    server = ".".join(
        str(part) for part in (info.server_version // 10000, info.server_version % 10000)
    )
    tunnel = getattr(session, "tunnel", None)
    if tunnel is not None:
        endpoint = tunnel.cfg.host
        if tunnel.cfg.remote_port:
            endpoint += f":{tunnel.cfg.remote_port}"
        host_value = _green(endpoint) + _c(f"   ⇄ via {tunnel.cfg.via} {tunnel.cfg.bastion}", "36")
    else:
        endpoint = info.host or "local"
        if info.host and info.port:
            endpoint += f":{info.port}"
        host_value = _green(endpoint)

    # When the profile has a reader endpoint, say which one we landed on.
    profile = getattr(session, "profile", None)
    mode = getattr(session, "mode", "")
    if profile is not None and profile.reader and mode:
        host_value += _red("  (writer)") if mode == "writer" else _c("  (reader)", "32")

    rows = [("db", _green(info.dbname or "")), ("host", host_value), ("user", _green(info.user or ""))]
    width = max(len(label) for label, _ in rows)
    body = "".join(f"  {_dim(label.ljust(width))}  {value}\n" for label, value in rows)
    return (
        f"pysql {__version__} {_dim('·')} PostgreSQL {server}\n"
        + body
        + f"Type {_dim('\\?')} for help, {_dim('\\q')} to quit. End statements with {_dim(';')}.\n"
    )


def _run_statement(conn: psycopg.Connection, sql: str, timing: bool, state: dict) -> bool:
    """Execute one SQL statement and print (or page) the outcome.

    Returns True on success, False if the statement raised (so a caller like
    ``\\f`` can summarise). Callers that don't care may ignore the result.
    """
    started = time.perf_counter()

    # EXPLAIN gets its own flame-graph-style renderer. We rewrite it to request
    # JSON and run *that* (never the original as well), so an ANALYZE executes
    # the query exactly once.
    explain = rewrite_explain(sql)
    if explain is not None:
        json_sql, analyze = explain
        try:
            with conn.cursor() as cur:
                cur.execute(json_sql)  # type: ignore[arg-type]
                raw = cur.fetchone()[0]  # type: ignore[index]
            elapsed_ms = (time.perf_counter() - started) * 1000
            data = raw if isinstance(raw, (list, dict)) else json.loads(raw)
            block = render_explain(data, analyze, _COLOR)
        except psycopg.Error as exc:
            _print_error(exc)
            return False
        except Exception:  # never re-run (side effects); fall back to raw JSON
            elapsed_ms = (time.perf_counter() - started) * 1000
            block = str(raw)
        tail = _dim(f"   {elapsed_ms:.1f} ms") + "\n" if timing else ""
        _emit(block.rstrip("\n") + "\n" + tail, state, check_width=False)
        return True

    table = None
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # type: ignore[arg-type]
            elapsed_ms = (time.perf_counter() - started) * 1000

            if cur.description is not None:
                columns = [d.name for d in cur.description]
                rows = cur.fetchall()
                table = render_table(
                    columns, rows,
                    header_sgr="1" if _COLOR else None,       # bold column titles
                    row_bgs=_ROW_BGS if _COLOR else None,     # zebra banding
                )
                summary = f"({len(rows)} row{'s' if len(rows) != 1 else ''})"
            else:
                # DDL / DML: psycopg exposes the server's command tag.
                summary = _green(cur.statusmessage or "OK")
    except psycopg.Error as exc:
        _print_error(exc)
        return False

    tail = summary
    if timing:
        tail += _dim(f"   {elapsed_ms:.1f} ms")

    if table is None:
        print(tail + "\n")           # a status line is never worth paging
    else:
        _emit(table + "\n" + tail + "\n", state)
    return True


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _emit(text: str, state: dict, check_width: bool = True) -> None:
    """Print ``text``, or send it through a pager when it won't fit the screen.

    ``check_width`` is for callers whose output is a wide *table* worth
    horizontal scrolling (query results). Describe views pass False: their
    tables are narrow and only a long prose title would be "wide", which is no
    reason to force a pager.
    """
    if _should_page(text, state, check_width):
        _page(text)
    else:
        print(text)


def _should_page(text: str, state: dict, check_width: bool = True) -> bool:
    if not state.get("pager", True) or not sys.stdout.isatty():
        return False
    size = shutil.get_terminal_size(fallback=(80, 24))
    lines = text.rstrip("\n").split("\n")                 # ignore trailing newline(s)
    if len(lines) > size.lines - 1:                       # too tall (leave a row for the prompt)
        return True
    if not check_width:
        return False
    widest = max((len(_ANSI_RE.sub("", ln)) for ln in lines), default=0)
    return widest > size.columns                          # too wide


def _page(text: str) -> None:
    """Feed ``text`` to $PAGER (default: less with horizontal scroll)."""
    pager = os.environ.get("PAGER") or "less -SRX"
    try:
        proc = subprocess.Popen(shlex.split(pager), stdin=subprocess.PIPE)
    except OSError:
        print(text)                                       # no pager available
        return
    try:
        proc.communicate(text.encode("utf-8", errors="replace"))
    except (BrokenPipeError, KeyboardInterrupt):
        # User quit the pager early (e.g. "q" in less); content was shown.
        proc.terminate()


def _print_error(exc: psycopg.Error) -> None:
    """Show an error with the diagnostics Postgres actually sent back."""
    diag = exc.diag
    header = _red("ERROR")
    if exc.sqlstate:
        header += _dim(f" [{exc.sqlstate}]")
    message = diag.message_primary or str(exc).strip()
    print(f"{header}: {message}")
    for label, value in (
        ("DETAIL", diag.message_detail),
        ("HINT", diag.message_hint),
    ):
        if value:
            print(f"  {_dim(label)}: {value}")
    if diag.statement_position:
        print(_dim(f"  at character {diag.statement_position}"))
    print()


# --- SQL behind the meta-commands ----------------------------------------

_DB_LIST_SQL = """\
SELECT d.datname AS database,
       pg_catalog.pg_get_userbyid(d.datdba) AS owner,
       CASE WHEN has_database_privilege(d.datname, 'CONNECT')
            THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname))
       END AS size
FROM pg_catalog.pg_database d
WHERE NOT d.datistemplate
ORDER BY d.datname;"""

# Size is the *estimated* size from catalog stats (relpages), not the exact
# pg_total_relation_size() — the latter takes an AccessShareLock per table, so a
# single locked table would block (hang) the whole listing. Estimates need no
# locks and are instant; exact sizes are in `\d+ <table>`.
_TABLE_LIST_SQL = """\
SELECT n.nspname AS schema,
       c.relname AS table,
       pg_catalog.pg_size_pretty((
           c.relpages
           + COALESCE(t.relpages, 0)
           + COALESCE((SELECT sum(i.relpages) FROM pg_catalog.pg_index x
                       JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid
                       WHERE x.indrelid = c.oid), 0)
       )::bigint * current_setting('block_size')::bigint) AS size,
       CASE WHEN c.relkind = 'p'
            THEN (SELECT count(*)::text FROM pg_catalog.pg_inherits inh WHERE inh.inhparent = c.oid)
            ELSE ''
       END AS partitions
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_class t ON t.oid = c.reltoastrelid
WHERE c.relkind IN ('r', 'p')              -- ordinary + partitioned tables
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND NOT c.relispartition                  -- hide partition children; mark parents instead
ORDER BY c.relname, n.nspname;"""

_EXTENSIONS_SQL = """\
SELECT e.extname                          AS name,
       e.extversion                       AS version,
       n.nspname                          AS schema,
       COALESCE(d.description, '')        AS description
FROM pg_catalog.pg_extension e
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
LEFT JOIN pg_catalog.pg_description d
       ON d.objoid = e.oid AND d.classoid = 'pg_catalog.pg_extension'::regclass
ORDER BY e.extname;"""

# Stats for one database. Reads cluster-global catalogs/views, so it works
# without switching into the database being inspected.
_DB_STAT_SQL = """\
SELECT d.datname                                   AS database,
       pg_catalog.pg_get_userbyid(d.datdba)        AS owner,
       pg_catalog.pg_encoding_to_char(d.encoding)  AS encoding,
       d.datcollate                                AS collation,
       d.datctype                                  AS ctype,
       d.datallowconn                              AS allows_connections,
       CASE WHEN d.datconnlimit < 0 THEN NULL ELSE d.datconnlimit END AS connection_limit,
       CASE WHEN has_database_privilege(d.datname, 'CONNECT')
            THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname))
       END                                         AS size,
       s.numbackends                               AS active_connections,
       s.xact_commit                               AS commits,
       s.xact_rollback                             AS rollbacks,
       CASE WHEN (s.blks_hit + s.blks_read) > 0
            THEN round(100.0 * s.blks_hit / (s.blks_hit + s.blks_read), 2)
       END                                         AS cache_hit_pct,
       s.tup_inserted                              AS rows_inserted,
       s.tup_updated                               AS rows_updated,
       s.tup_deleted                               AS rows_deleted,
       s.deadlocks,
       s.stats_reset
FROM pg_catalog.pg_database d
LEFT JOIN pg_catalog.pg_stat_database s ON s.datname = d.datname
WHERE d.datname = %s;"""

# The \d <table> detail is built from several queries. to_regclass() resolves
# the name through the search_path and yields NULL (no error) when nothing
# matches, which is how we detect "no such table".

# One-row header: identity and sizes for the title line.
_TABLE_HEADER_SQL = """\
SELECT n.nspname                                   AS schema,
       c.relname                                   AS name,
       pg_catalog.pg_get_userbyid(c.relowner)      AS owner,
       CASE c.relkind WHEN 'r' THEN 'table'
                      WHEN 'p' THEN 'partitioned table'
                      WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized view'
                      WHEN 'f' THEN 'foreign table'
                      ELSE c.relkind::text END     AS kind,
       c.reltuples::bigint                         AS est_rows,
       pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS total_size,
       pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid))          AS table_size,
       pg_catalog.pg_size_pretty(pg_catalog.pg_indexes_size(c.oid))        AS indexes_size
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass(%s);"""

# Columns, with resolved type, nullability and default.
_COLUMNS_SQL = """\
SELECT a.attname                                   AS column,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
       CASE WHEN a.attnotnull THEN 'not null' ELSE '' END AS nullable,
       pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS default
FROM pg_catalog.pg_attribute a
LEFT JOIN pg_catalog.pg_attrdef ad
       ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE a.attrelid = to_regclass(%s)
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;"""

# Indexes, with the primary/unique role folded in and a compact definition
# (everything after "USING", e.g. "btree (email)").
_INDEXES_SQL = """\
SELECT c2.relname                                  AS index,
       CASE WHEN i.indisprimary THEN 'primary key'
            WHEN i.indisunique  THEN 'unique'
            ELSE '' END                            AS role,
       COALESCE(s.idx_scan, 0)                     AS scans,
       regexp_replace(pg_catalog.pg_get_indexdef(i.indexrelid), '^.*? USING ', '') AS definition
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c2 ON c2.oid = i.indexrelid
LEFT JOIN pg_catalog.pg_stat_all_indexes s ON s.indexrelid = i.indexrelid
WHERE i.indrelid = to_regclass(%s)
ORDER BY i.indisprimary DESC, c2.relname;"""

# Constraints that aren't just represented by an index: foreign keys, checks,
# exclusions. (Primary/unique already show up in the Indexes section.)
_CONSTRAINTS_SQL = """\
SELECT conname                                     AS name,
       CASE contype WHEN 'f' THEN 'foreign key'
                    WHEN 'c' THEN 'check'
                    WHEN 'x' THEN 'exclude'
                    ELSE contype::text END          AS type,
       pg_catalog.pg_get_constraintdef(oid, true)  AS definition
FROM pg_catalog.pg_constraint
WHERE conrelid = to_regclass(%s)
  AND contype IN ('f', 'c', 'x')
ORDER BY contype, conname;"""

# Runtime activity from the stats collector (ordinary tables only).
_TABLE_ACTIVITY_SQL = """\
SELECT seq_scan   AS sequential_scans,
       idx_scan   AS index_scans,
       n_live_tup AS live_rows,
       n_dead_tup AS dead_rows,
       last_vacuum,
       last_autovacuum,
       last_analyze,
       last_autoanalyze
FROM pg_catalog.pg_stat_user_tables
WHERE relid = to_regclass(%s);"""

# Metadata used to feed tab-completion. Cheap, run only on connect / switch.
_META_DATABASES_SQL = (
    "SELECT datname FROM pg_catalog.pg_database "
    "WHERE NOT datistemplate ORDER BY 1;"
)
_META_TABLES_SQL = """\
SELECT DISTINCT tablename FROM pg_catalog.pg_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;"""
# table -> column, so completion can scope columns to the tables in a query.
_META_COLUMNS_SQL = """\
SELECT c.relname, a.attname
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY c.relname, a.attnum;"""
_META_ROLES_SQL = "SELECT rolname FROM pg_catalog.pg_roles ORDER BY 1;"
_META_SEQUENCES_SQL = """\
SELECT DISTINCT sequencename FROM pg_catalog.pg_sequences
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;"""

# Role attributes and the membership graph, for \du. pg_roles is readable by any
# user (unlike pg_authid, which hides password hashes behind superuser).
_ROLES_SQL = """\
SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
       rolcreaterole, rolreplication, rolbypassrls, rolconnlimit,
       rolvaliduntil::text AS rolvaliduntil
FROM pg_catalog.pg_roles
ORDER BY rolname;"""
_ROLE_MEMBERS_SQL = """\
SELECT g.rolname AS grp, m.rolname AS member, am.admin_option
FROM pg_catalog.pg_auth_members am
JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
JOIN pg_catalog.pg_roles m ON m.oid = am.member
ORDER BY g.rolname, m.rolname;"""

# --- object privileges (\dp) ---------------------------------------------
# Effective access is computed by Postgres itself (has_*_privilege), which
# resolves group membership, inheritance, PUBLIC and ownership for us.
_PRIV_SUPERUSERS_SQL = (
    "SELECT rolname FROM pg_catalog.pg_roles "
    "WHERE rolsuper AND rolname NOT LIKE 'pg\\_%' ORDER BY 1;"
)
# Schema grid: one row per (schema, non-super role) with usage/create booleans.
_PRIV_SCHEMA_GRID_SQL = """\
SELECT n.nspname, r.rolname,
       has_schema_privilege(r.oid, n.oid, 'USAGE'),
       has_schema_privilege(r.oid, n.oid, 'CREATE')
FROM pg_catalog.pg_namespace n
CROSS JOIN pg_catalog.pg_roles r
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%'
  AND r.rolname NOT LIKE 'pg\\_%' AND NOT r.rolsuper
ORDER BY 1, 2;"""
# PUBLIC's own schema grants (grantee 0 in the ACL).
_PRIV_SCHEMA_PUBLIC_SQL = """\
SELECT n.nspname, a.privilege_type
FROM pg_catalog.pg_namespace n,
     LATERAL aclexplode(n.nspacl) a
WHERE a.grantee = 0
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%';"""
# Table grid: one row per (table, non-super role) with the seven booleans.
_PRIV_TABLE_GRID_SQL = """\
SELECT n.nspname, c.relname, r.rolname,
       has_table_privilege(r.oid, c.oid, 'SELECT'),
       has_table_privilege(r.oid, c.oid, 'INSERT'),
       has_table_privilege(r.oid, c.oid, 'UPDATE'),
       has_table_privilege(r.oid, c.oid, 'DELETE'),
       has_table_privilege(r.oid, c.oid, 'TRUNCATE'),
       has_table_privilege(r.oid, c.oid, 'REFERENCES'),
       has_table_privilege(r.oid, c.oid, 'TRIGGER')
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN pg_catalog.pg_roles r
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND r.rolname NOT LIKE 'pg\\_%' AND NOT r.rolsuper
ORDER BY 1, 2, 3;"""
_PRIV_TABLE_PUBLIC_SQL = """\
SELECT n.nspname, c.relname, a.privilege_type
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
     LATERAL aclexplode(c.relacl) a
WHERE a.grantee = 0
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema');"""

# Per-table detail: metadata, effective access per role, direct grants.
_PRIV_TABLE_META_SQL = """\
SELECT n.nspname, c.relname, pg_catalog.pg_get_userbyid(c.relowner) AS owner
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass(%s);"""
_PRIV_TABLE_EFFECTIVE_SQL = """\
SELECT r.rolname,
       has_table_privilege(r.oid, to_regclass(%s), 'SELECT'),
       has_table_privilege(r.oid, to_regclass(%s), 'INSERT'),
       has_table_privilege(r.oid, to_regclass(%s), 'UPDATE'),
       has_table_privilege(r.oid, to_regclass(%s), 'DELETE'),
       has_table_privilege(r.oid, to_regclass(%s), 'TRUNCATE'),
       has_table_privilege(r.oid, to_regclass(%s), 'REFERENCES'),
       has_table_privilege(r.oid, to_regclass(%s), 'TRIGGER')
FROM pg_catalog.pg_roles r
WHERE r.rolname NOT LIKE 'pg\\_%%' AND NOT r.rolsuper;"""
_PRIV_TABLE_DIRECT_SQL = """\
SELECT COALESCE(g.rolname, 'PUBLIC') AS grantee, a.privilege_type
FROM pg_catalog.pg_class c,
     LATERAL aclexplode(c.relacl) a
LEFT JOIN pg_catalog.pg_roles g ON g.oid = a.grantee
WHERE c.oid = to_regclass(%s);"""

# Per-role detail: schema and table access for one role name.
_PRIV_ROLE_SCHEMAS_SQL = """\
SELECT n.nspname,
       has_schema_privilege(%s, n.oid, 'USAGE'),
       has_schema_privilege(%s, n.oid, 'CREATE')
FROM pg_catalog.pg_namespace n
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%%'
ORDER BY 1;"""
_PRIV_ROLE_TABLES_SQL = """\
SELECT n.nspname, c.relname,
       has_table_privilege(%s, c.oid, 'SELECT'),
       has_table_privilege(%s, c.oid, 'INSERT'),
       has_table_privilege(%s, c.oid, 'UPDATE'),
       has_table_privilege(%s, c.oid, 'DELETE'),
       has_table_privilege(%s, c.oid, 'TRUNCATE'),
       has_table_privilege(%s, c.oid, 'REFERENCES'),
       has_table_privilege(%s, c.oid, 'TRIGGER')
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1, 2;"""
_PRIV_ROLE_EXISTS_SQL = "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = %s;"


class DBSession:
    """Owns the live connection and can switch databases in place.

    Switching keeps the host, user and credentials from the original
    connection info; only the database name changes. The most recent database
    is remembered so ``\\c -`` can jump back to it.

    Also caches lightweight metadata (database / table / column names) for
    tab-completion, refreshed on connect and on every switch.
    """

    def __init__(self, source=None, tunnel=None, profile: Profile | None = None,
                 mode: str = "") -> None:
        # ``source`` is a conninfo string, or a zero-arg callable returning one.
        # A callable is re-invoked on every (re)connect, so time-limited
        # credentials like RDS IAM tokens are regenerated fresh each time.
        # ``tunnel`` (optional) is an SSM port-forward brought up for the life of
        # this session and torn down in close().
        # ``profile`` (optional) makes this a profile session with reader/writer
        # awareness: ``mode`` ("reader"/"writer") selects the endpoint, and the
        # conninfo is rebuilt from it on every (re)connect.
        self.profile = profile
        self.mode = mode
        self.tunnel = tunnel
        self._provider = self._build_conninfo if profile is not None else (
            source if callable(source) else (lambda s=source: s))
        if tunnel is not None and profile is not None and profile.reader:
            # Aim the forward at the endpoint the initial mode selects.
            tunnel.point_at(_endpoint_host(profile, mode))
        self.previous_dbname: str | None = None
        self.databases: list[str] = []
        self.tables: list[str] = []
        self.columns: list[str] = []
        self.table_columns: dict[str, list[str]] = {}
        self.roles: list[str] = []
        self.sequences: list[str] = []
        if tunnel is not None:
            tunnel.start()  # blocks until the local port is ready; raises TunnelError
        try:
            self.conn = psycopg.connect(self._provider(), autocommit=True)
        except BaseException:
            if tunnel is not None:  # don't leak the tunnel if the DB connect fails
                tunnel.stop()
            raise
        self.load_metadata()

    def _build_conninfo(self) -> str:
        """Conninfo for the current reader/writer mode (profile sessions only)."""
        endpoint = _endpoint_host(self.profile, self.mode)
        if self.tunnel is not None:
            host, port = self.tunnel.endpoint()
            return profile_conninfo(self.profile, endpoint_host=endpoint,
                                    connect_host=host, connect_port=port,
                                    remote_port=self.tunnel.cfg.remote_port)
        return profile_conninfo(self.profile, endpoint_host=endpoint)

    def close(self) -> None:
        """Close the DB connection and stop the tunnel (if any)."""
        try:
            self.conn.close()
        finally:
            if self.tunnel is not None:
                self.tunnel.stop()

    def switch(self, target: str) -> str:
        """Reconnect to ``target`` ('-' means the previous database).

        On success the old connection is closed and the new database name is
        returned. On failure the current connection is left untouched and the
        original error propagates.
        """
        if target == "-":
            if not self.previous_dbname:
                raise ValueError("No previous database to connect to.")
            target = self.previous_dbname

        current = self.conn.info.dbname
        # Re-resolve the conninfo (fresh IAM token if applicable); the dbname
        # keyword overrides whatever database it names.
        new_conn = psycopg.connect(self._provider(), dbname=target, autocommit=True)
        self.conn.close()
        self.conn = new_conn
        self.previous_dbname = current
        self.load_metadata()
        return target

    def switch_mode(self, mode: str) -> str:
        """Switch this session between the ``reader`` and ``writer`` endpoint.

        Reconnects to the other endpoint (restarting the SSM tunnel onto it first,
        if there is one). Mirrors :meth:`switch`/:meth:`rebind_tunnel`: on a plain
        failure the current connection is left working on its old endpoint; only a
        tunnel rollback that itself fails leaves the connection closed
        (``self.conn.closed`` is then true). Returns the new mode.
        """
        if self.profile is None or not self.profile.reader:
            raise ValueError("this connection has no reader endpoint to switch to.")
        if mode == self.mode:
            return self.mode
        dbname = self.conn.info.dbname
        old_mode = self.mode
        if self.tunnel is not None:
            # Re-point the forward first; restores the old host itself on failure.
            self.tunnel.repoint(_endpoint_host(self.profile, mode))
        self.mode = mode
        try:
            new_conn = psycopg.connect(self._provider(), dbname=dbname, autocommit=True)
        except Exception as exc:
            self.mode = old_mode
            if self.tunnel is not None:
                # The tunnel moved; the old connection is now dead. Put the tunnel
                # back and reconnect so the session survives on its old endpoint.
                try:
                    self.tunnel.repoint(_endpoint_host(self.profile, old_mode))
                    restored = psycopg.connect(self._provider(), dbname=dbname, autocommit=True)
                except Exception:
                    self.conn.close()
                    raise TunnelError(f"could not switch to {mode} and failed to restore "
                                      f"{old_mode}; the session is now closed.") from exc
                self.conn.close()
                self.conn = restored
            raise  # the current connection is intact on the old endpoint
        self.conn.close()
        self.conn = new_conn
        self.load_metadata()
        return self.mode

    def rebind_tunnel(self, local_port: int) -> int:
        """Move this session's SSM tunnel to ``local_port`` (0 picks a free one)
        and reconnect through it, so other apps can reach this same database at
        ``127.0.0.1:<port>``. Returns the new local port.

        The tunnel's new listener is brought up before the old one is dropped,
        so a bad or busy port raises :class:`TunnelError` with the session left
        untouched. If the port opens but the database reconnect then fails, the
        tunnel is put back on its old port and a :class:`TunnelError` is raised
        with the session still live on it; only if that rollback also fails is
        the connection left closed (``self.conn.closed`` is then true).
        """
        if self.tunnel is None:
            raise TunnelError("this session has no tunnel to move — the profile "
                              "connects directly, without an SSM tunnel.")
        dbname = self.conn.info.dbname
        old_port = self.tunnel.local_port
        self.tunnel.rebind(local_port)  # new listener up, old retired; raises on bind failure
        try:
            new_conn = psycopg.connect(self._provider(), dbname=dbname, autocommit=True)
        except Exception as exc:
            # The port moved but the database wouldn't reconnect — put it back.
            try:
                self.tunnel.rebind(old_port)
                self.conn.close()
                self.conn = psycopg.connect(self._provider(), dbname=dbname, autocommit=True)
            except Exception:
                self.conn.close()  # can't recover — leave the connection closed
                raise TunnelError(f"moved the tunnel but could not reconnect "
                                  f"({exc}); the session is now closed.") from exc
            raise TunnelError(f"could not reconnect on local port {self.tunnel.local_port} "
                              f"({exc}); left the tunnel on its previous port "
                              f"{old_port}.") from exc
        self.conn.close()
        self.conn = new_conn
        self.load_metadata()
        return self.tunnel.local_port

    def load_metadata(self) -> None:
        """Refresh the cached names used by tab-completion."""
        self.databases = self._fetch_names(_META_DATABASES_SQL)
        self.tables = self._fetch_names(_META_TABLES_SQL)
        self.roles = self._fetch_names(_META_ROLES_SQL)
        self.sequences = self._fetch_names(_META_SEQUENCES_SQL)
        self.table_columns, self.columns = self._fetch_columns()

    def _fetch_names(self, sql: str) -> list[str]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                return [row[0] for row in cur.fetchall()]
        except psycopg.Error:
            # Completion is a convenience; never let it break the session.
            return []

    def _fetch_columns(self) -> tuple[dict[str, list[str]], list[str]]:
        table_columns: dict[str, list[str]] = {}
        try:
            with self.conn.cursor() as cur:
                cur.execute(_META_COLUMNS_SQL)
                for table, column in cur.fetchall():
                    table_columns.setdefault(table, []).append(column)
        except psycopg.Error:
            return {}, []
        all_columns = sorted({c for cols in table_columns.values() for c in cols})
        return table_columns, all_columns


class SessionManager:
    """Holds several open connections at once and tracks which is current.

    One session is the "current" one every statement runs against. ``\\C``
    opens or switches between named profile sessions, ``\\C -`` toggles to the
    previous one, and ``\\close`` closes the current one. The startup connection
    is just another session (named after its database); profile sessions carry
    their profile name.
    """

    def __init__(self, initial: DBSession, name: str, *, is_profile: bool = False) -> None:
        self.sessions: dict[str, DBSession] = {name: initial}
        self.profiles: set[str] = {name} if is_profile else set()
        self.current = name
        self.previous: str | None = None

    @property
    def session(self) -> DBSession:
        return self.sessions[self.current]

    def _goto(self, name: str) -> None:
        if name != self.current:
            self.previous = self.current
            self.current = name

    def add(self, name: str, sess: DBSession) -> None:
        self.sessions[name] = sess
        self.profiles.add(name)
        self._goto(name)

    def switch_existing(self, name: str) -> None:
        self._goto(name)

    def switch_previous(self) -> str:
        if not self.previous or self.previous not in self.sessions:
            raise ValueError("No previous profile to switch back to.")
        self.current, self.previous = self.previous, self.current
        return self.current

    def close_current(self) -> str:
        if len(self.sessions) == 1:
            raise ValueError(r"Can't close the only open session — use \q to quit.")
        name = self.current
        self.sessions.pop(name).close()
        self.profiles.discard(name)
        self.current = self.previous if self.previous in self.sessions else next(iter(self.sessions))
        self.previous = None
        return self.current

    def close_all(self) -> None:
        for sess in self.sessions.values():
            try:
                sess.close()
            except Exception:
                pass


def _connect_meta(session: DBSession, args: list[str]) -> None:
    if not args:
        print(_red(r"usage: \c <database>   (\c - connects to the previous one)") + "\n")
        return
    try:
        now = session.switch(args[0])
    except ValueError as exc:
        print(_red(str(exc)) + "\n")
        return
    except psycopg.Error as exc:
        # Reconnect failed; the previous connection is still live.
        print(_red("Could not connect: ") + str(exc).strip() + "\n")
        return
    print(
        _green(f'You are now connected to database "{now}" '
               f'as user "{session.conn.info.user}".') + "\n"
    )


def _handle_rebind_port(manager: SessionManager, args: list[str]) -> None:
    """``\\P <port>`` — pin this session's SSM tunnel to a fixed local port.

    Reconnects the session through ``127.0.0.1:<port>`` (``0`` picks a free
    port) so other applications can reach the same database over that port.
    Only works for profiles opened through an SSM tunnel.
    """
    session = manager.session
    if session.tunnel is None:
        print(_red(r"\P needs a tunnelled session")
              + _dim(" — the current profile connects directly, with no SSM tunnel to move.")
              + "\n")
        return
    if len(args) != 1:
        print(_red(r"usage: \P <port>") + _dim("  (0 picks a free port)") + "\n")
        return
    try:
        port = int(args[0])
    except ValueError:
        print(_red(f"Not a port number: {args[0]}") + "\n")
        return
    if port != 0 and not (1 <= port <= 65535):
        print(_red(f"Port out of range (1–65535): {port}") + "\n")
        return
    if port != 0 and port == session.tunnel.local_port:
        print(_dim(f"Tunnel is already on local port {port}.") + "\n")
        return

    old_port = session.tunnel.local_port
    print(_dim(f"Moving tunnel from local port {old_port} to "
               f"{port or 'an auto-picked port'} …"), flush=True)
    try:
        new_port = session.rebind_tunnel(port)
    except TunnelError as exc:
        print(_red("Could not move the tunnel: ") + str(exc).strip() + "\n")
        if session.conn.closed:
            # The session died mid-move; drop it so the prompt isn't left broken.
            try:
                now = manager.close_current()
                _announce_session(manager, now)
            except ValueError:
                print(_red(r"The last session is now closed — use \q to quit.") + "\n")
        return
    print(_green(f"Tunnel now on 127.0.0.1:{new_port}")
          + _dim(f"  — point your apps at localhost:{new_port} "
                 f"({session.tunnel.describe()})") + "\n")


def _handle_toggle_rw(manager: SessionManager) -> None:
    """``\\w`` — toggle the current session between its reader and writer endpoint."""
    session = manager.session
    profile = session.profile
    if profile is None or not profile.reader:
        print(_red(r"\w needs a reader endpoint")
              + _dim(" — this connection has no `reader:` host to toggle to.") + "\n")
        return
    target = "writer" if session.mode == "reader" else "reader"
    endpoint = _endpoint_host(profile, target)
    print(_dim(f"Switching to the {target} ({endpoint}) …"), flush=True)
    try:
        now = session.switch_mode(target)
    except (TunnelError, psycopg.Error) as exc:
        print(_red("Could not switch: ") + str(exc).strip() + "\n")
        if session.conn.closed:  # tunnel rollback failed — the session is gone
            try:
                nm = manager.close_current()
                _announce_session(manager, nm)
            except ValueError:
                print(_red(r"The last session is now closed — use \q to quit.") + "\n")
        return
    role = _red("writer") if now == "writer" else _green("reader")
    print(f"Now on the {role} endpoint " + _dim(f"({_endpoint_host(profile, now)}).") + "\n")


def _connect_profile(manager: SessionManager, args: list[str]) -> None:
    """``\\C`` — list profiles, or open/switch to one (``\\C -`` = previous)."""
    if not args:
        _list_profiles(manager)
        return
    target = args[0]

    if target == "-":
        try:
            now = manager.switch_previous()
        except ValueError as exc:
            print(_red(str(exc)) + "\n")
            return
        _announce_session(manager, now)
        return

    if target in manager.sessions:  # already open — just switch to it
        manager.switch_existing(target)
        _announce_session(manager, target)
        return

    profiles = load_profiles()
    if target not in profiles:
        print(_red(f'No profile named "{target}" in {active_profile_path()}.') + "\n")
        return
    sess, err = _connect_profile_session(target, profiles[target])
    if err is not None:
        if isinstance(err, psycopg.Error):
            print(_red(f"Could not connect to {target}: ") + str(err).strip() + "\n")
        else:
            print(_red(f"Profile {target}: ") + str(err) + "\n")
        return
    manager.add(target, sess)
    _announce_session(manager, target, opened=True)


def _endpoint_host(profile: Profile, mode: str) -> str:
    """The real server hostname for ``mode`` — the reader when in reader mode and
    a reader is configured, otherwise the writer (``host:``)."""
    if mode == "reader" and profile.reader:
        return profile.reader
    return profile.hostname


def _initial_mode(profile: Profile) -> str:
    """The endpoint a profile lands on. Reader-enabled profiles default to the
    reader (``default_mode: writer`` overrides); others have no mode (``""``)."""
    if not profile.reader:
        return ""
    return profile.default_mode or "reader"


def _open_profile(profile: Profile):
    """Bring up a profile's tunnel (if any) and connect.

    Returns ``(session, None)`` on success or ``(None, exception)`` on failure.
    A fresh tunnel is created per call (DBSession tears its own down on failure),
    so this is safe to call again for a retry.
    """
    try:
        tunnel = tunnel_from_profile(profile)
    except TunnelError as exc:
        return None, exc
    mode = _initial_mode(profile)
    if tunnel is not None:
        if profile.reader:  # show the endpoint the initial mode actually uses
            tunnel.point_at(_endpoint_host(profile, mode))
        print(_dim(f"Opening SSM tunnel ({tunnel.describe()}) …"), flush=True)
    try:
        return DBSession(tunnel=tunnel, profile=profile, mode=mode), None
    except (crypto.CryptoError, rds.RdsAuthError, TunnelError, psycopg.Error) as exc:
        return None, exc


def _wants_password(profile: Profile, exc: Exception) -> bool:
    """True if ``exc`` is the server asking for a password the profile lacks.

    Only for plain (non-IAM) profiles with no stored password — the case the user
    can fix by typing one. A wrong *stored* password is left for them to correct.
    """
    if profile.auth == "rds-iam" or profile.password:
        return False
    msg = str(exc).lower()
    return ("no password supplied" in msg
            or "password authentication failed" in msg
            or "fe_sendauth" in msg)


def _connect_profile_session(target: str, profile: Profile):
    """Open ``profile``'s session, prompting for a password if the server needs
    one the profile doesn't have — and, on success, saving it (encrypted) back
    into the profile so it isn't asked for again. Returns ``(session, error)``.
    """
    sess, err = _open_profile(profile)
    if err is None or not _wants_password(profile, err):
        return sess, err

    print(_dim(f"Profile {target} has no stored password and the server requires one."))
    try:
        password = getpass.getpass(f"Password for {target}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return sess, err  # keep the original error; nothing entered
    if not password:
        print(_dim("  (no password entered — not retrying)"))
        return sess, err

    trial = replace(profile, password=crypto.encrypt(password))
    sess, err = _open_profile(trial)
    if err is None:
        try:
            set_profile_password(target, trial.password)
            print(_green(f"Saved the password to profile {target}")
                  + _dim(f"  (encrypted, in {active_profile_path()})"))
        except (OSError, KeyError, ValueError) as exc:
            print(_dim(f"  (connected, but couldn't save it to the profile: {exc})"))
    return sess, err


_PROFILE_NAME_RE = re.compile(r"^[\w.\-]+$")


def _looks_like_iam(password: str) -> bool:
    """True if ``password`` is an RDS IAM auth token.

    An IAM token is a SigV4-presigned URL (``…/?Action=connect&…X-Amz-Signature=…``);
    an ordinary password essentially never contains that signature parameter.
    """
    return "X-Amz-Signature=" in password and "Action=connect" in password


def _region_from_host(host: str) -> str:
    """Best-effort AWS region from an RDS endpoint, or '' if it isn't one.

    e.g. ``mycluster.cluster-xxxx.ap-southeast-2.rds.amazonaws.com`` → ``ap-southeast-2``.
    """
    parts = (host or "").split(".")
    if "rds" in parts:
        i = parts.index("rds")
        if i > 0:
            return parts[i - 1]
    return ""


def _ask(question: str, default: str = "") -> str | None:
    """Prompt for a single line; return it (or ``default`` if blank).

    Returns ``None`` on EOF/Ctrl-C so the caller can abort cleanly.
    """
    try:
        answer = input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer or default


def _add_iam_profile(name: str, info) -> None:
    """``\\add`` for an RDS IAM connection: prompt for the AWS fields, never the token."""
    print(_green("This looks like an RDS IAM connection.")
          + _dim("  The auth token is short-lived, so it won't be saved;")
          + _dim("\n  pysql will regenerate one per-connect from your AWS credentials.") + "\n")

    region = _ask(
        _bold("AWS region")
        + _dim(f" [{_region_from_host(info.host) or 'none'}]: "),
        default=_region_from_host(info.host),
    )
    if region is None:
        print(_dim("  (cancelled — nothing saved)") + "\n")
        return
    aws_profile = _ask(_bold("AWS profile") + _dim(" (blank = default creds): "))
    if aws_profile is None:
        print(_dim("  (cancelled — nothing saved)") + "\n")
        return
    sslmode = _ask(_bold("sslmode") + _dim(" [require]: "), default="require")
    if sslmode is None:
        print(_dim("  (cancelled — nothing saved)") + "\n")
        return

    profile = Profile(
        name=name,
        username=info.user or "",
        password="",
        hostname=info.host or "",
        port=str(info.port or ""),
        database=info.dbname or "",
        auth="rds-iam",
        region=region,
        aws_profile=aws_profile,
        sslmode=sslmode,
    )
    try:
        save_profile(profile)
    except OSError as exc:
        print(_red(f"Could not write {active_profile_path()}: ") + str(exc) + "\n")
        return

    where = f"{info.user}@{info.host or 'local'}/{info.dbname}"
    print(_green(f"Saved RDS IAM profile {name}") + _dim(f"  ({where})")
          + _dim(f"\n  connect with \\C {name}") + "\n")


def _add_profile(manager: SessionManager, args: list[str]) -> None:
    """``\\add <name>`` — save the current connection as a profile."""
    if not args:
        print(_red(r"usage: \add <profile_name>") + "\n")
        return
    if len(args) > 1 or not _PROFILE_NAME_RE.match(args[0]):
        print(_red("Profile name must be a single token of letters, digits, "
                   "'.', '-' or '_'.") + "\n")
        return
    name = args[0]
    if name in load_profiles():
        print(_red(f'Profile "{name}" already exists in {active_profile_path()}.')
              + _dim("  (remove it first, or pick another name)") + "\n")
        return

    info = manager.session.conn.info
    password = info.password or ""
    if _looks_like_iam(password):
        _add_iam_profile(name, info)
        return

    profile = Profile(
        name=name,
        username=info.user or "",
        password=crypto.encrypt(password) if password else "",
        hostname=info.host or "",
        port=str(info.port or ""),
        database=info.dbname or "",
    )
    try:
        save_profile(profile)
    except OSError as exc:
        print(_red(f"Could not write {active_profile_path()}: ") + str(exc) + "\n")
        return

    where = f"{info.user}@{info.host or 'local'}/{info.dbname}"
    msg = _green(f"Saved profile {name}") + _dim(f"  ({where})")
    if not password:
        msg += _dim("\n  (no password captured — connect used trust/peer/.pgpass; "
                    r"add one later with \enc)")
    print(msg + "\n")


def _announce_session(manager: SessionManager, name: str, *, opened: bool = False) -> None:
    sess = manager.sessions[name]
    info = sess.conn.info
    verb = "Connected to" if opened else "Switched to"
    where = f"{info.user}@{info.host or 'local'}/{info.dbname}"
    msg = _green(f"{verb} {name}") + _dim(f"  ({where})")
    if getattr(sess, "tunnel", None) is not None:
        msg += _c(f"  ⇄ via {sess.tunnel.describe()}", "36")
    print(msg + "\n")


def _list_profiles(manager: SessionManager) -> None:
    profiles = load_profiles()
    # Connected first (everything open — profiles and ad-hoc sessions alike),
    # then the profiles from the file that aren't open yet.
    open_names = sorted(manager.sessions)
    closed = sorted(n for n in profiles if n not in manager.sessions)
    # Pad names to a common width so the target column lines up across both lists.
    width = max((len(n) for n in list(profiles) + open_names), default=0)
    cont_indent = " " * (width + 7)  # aligns a wrapped detail line under `target`

    def row(name: str, target: str, *, is_open: bool, is_current: bool, extra: str = "") -> str:
        bullet = "→" if is_current else ("●" if is_open else "·")
        label = (_green(name) if is_open else name) + " " * (width - len(name))
        tag = "  (current)" if is_current else ""
        line = f"  {bullet} {label}   {_dim(target + tag)}"
        # Long tunnel detail wraps messily on the main line, so give it its own.
        return line + ("\n" + cont_indent + extra if extra else "")

    lines = [_dim(f"Profiles ({active_profile_path()}):"), ""]

    lines.append(_bold("Connected") if open_names else _dim("Connected: (none)"))
    for name in open_names:
        sess = manager.sessions[name]
        info = sess.conn.info
        if name in profiles:  # show where it *points*, not the local tunnel endpoint
            p = profiles[name]
            target = f"{p.username or info.user or '?'}@{p.hostname or info.host or 'local'}/{p.database or info.dbname or '?'}"
        else:
            target = f"{info.user}@{info.host or 'local'}/{info.dbname}"
        tun = getattr(sess, "tunnel", None)
        extra = _c(f"⇄ {tun.describe()}  (localhost:{tun.local_port})", "36") if tun else ""
        lines.append(row(name, target, is_open=True,
                         is_current=name == manager.current, extra=extra))

    if closed:
        lines.append("")
        lines.append(_bold("Not connected"))
        for name in closed:
            p = profiles[name]
            # Target disambiguates clusters that share a database name; mark the
            # ones that tunnel so they're identifiable even while closed.
            target = f"{p.username or '?'}@{p.hostname or 'local'}/{p.database or '?'}"
            if p.tunnel:
                target += " ⇄ ssm"
            lines.append(row(name, target, is_open=False, is_current=False))
    elif not profiles:
        lines.append(_dim("  (no profiles yet — add some to the file; \\enc makes passwords)"))

    print("\n".join(lines) + "\n")


def _show_detail(conn: psycopg.Connection, sql: str, param: str,
                 missing: str, state: dict) -> None:
    """Run a single-row stats query and page it as a field/value table."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (param,))
            columns = [d.name for d in cur.description]
            row = cur.fetchone()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if row is None:
        print(_red(missing) + "\n")
        return
    # One object reads far better vertically than as a very wide row.
    _emit(render_table(["field", "value"], list(zip(columns, row, strict=False)),
                       header_sgr=_header_sgr()) + "\n",
          state, check_width=False)


def _format_section(title: str, columns, rows) -> str:
    """Return a labelled table (bold label + bold column headers), or '' if empty."""
    if not rows:
        return ""
    return (_bold(title + ":") + "\n"
            + render_table(columns, rows, header_sgr=_header_sgr()) + "\n\n")


def _index_usage_cell(scans: int, total: int, role: str) -> str:
    """Colour an index by how heavily it's used, relative to the table's others.

    Unused indexes are the actionable case: red when they could be dropped, but
    only amber when they back a primary-key/unique constraint (still needed for
    integrity even if never read).
    """
    if scans == 0:
        if role in ("primary key", "unique"):
            return _c("0 — unused for reads (enforces key)", "33")
        return _red("0 — unused, consider dropping")
    label = f"{scans:,} scans"
    share = scans / total if total else 0
    if share >= 0.25:
        return _green(label)          # a major share of this table's index scans
    if share >= 0.05:
        return _c(label, "33")        # used, but a minor share
    return _c(label, "31")            # rarely used relative to the others


# Partition metadata: the key of a partitioned parent, this table's parent (if
# it is a partition), and a parent's child partitions with bounds and size.
_PARTKEY_SQL = "SELECT pg_catalog.pg_get_partkeydef(to_regclass(%s));"
_PARTITION_OF_SQL = """\
SELECT n.nspname || '.' || p.relname AS parent,
       pg_catalog.pg_get_expr(c.relpartbound, c.oid) AS bounds
FROM pg_catalog.pg_inherits i
JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
JOIN pg_catalog.pg_namespace n ON n.oid = p.relnamespace
WHERE i.inhrelid = to_regclass(%s);"""
_PARTITIONS_SQL = """\
SELECT c.relname AS partition,
       pg_catalog.pg_get_expr(c.relpartbound, c.oid) AS bounds,
       pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS size
FROM pg_catalog.pg_inherits i
JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
WHERE i.inhparent = to_regclass(%s)
ORDER BY c.relname;"""


def _handle_table_list(session: DBSession, state: dict) -> None:
    """``\\d`` with no argument — list tables (estimated size; parents marked)."""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_TABLE_LIST_SQL)
            cols = [d.name for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not rows:
        print(_dim("No tables in this database.") + "\n")
        return
    # Drop the 'partitions' column when nothing is partitioned, so it's not a
    # column of blanks on the common case.
    if cols[-1] == "partitions" and not any(r[-1] for r in rows):
        cols, rows = cols[:-1], [r[:-1] for r in rows]
    summary = f"({len(rows)} row{'s' if len(rows) != 1 else ''})"
    _emit(render_table(cols, rows, header_sgr=_header_sgr()) + "\n" + summary + "\n", state)


def _describe_table(session: DBSession, name: str, state: dict, *, verbose: bool = False) -> None:
    """Show a table's structure.

    Basic (``\\d``): columns plus any partition information. Verbose (``\\d+``)
    adds indexes, constraints, sizes and activity.
    """
    conn = session.conn
    try:
        with conn.cursor() as cur:
            cur.execute(_TABLE_HEADER_SQL, (name,))
            header_row = cur.fetchone()
            if header_row is None:
                print(_red(f'No table named "{name}" in the current database '
                           "(try schema.table).") + "\n")
                return
            h = dict(zip((d.name for d in cur.description), header_row, strict=False))

            cur.execute(_COLUMNS_SQL, (name,))
            col_cols = [d.name for d in cur.description]
            col_rows = cur.fetchall()

            cur.execute(_PARTITION_OF_SQL, (name,))
            parent = cur.fetchone()                 # (parent, bounds) or None
            cur.execute(_PARTKEY_SQL, (name,))
            partkey = cur.fetchone()[0]             # set only for a partitioned parent
            partitions = []
            if partkey:
                cur.execute(_PARTITIONS_SQL, (name,))
                partitions = cur.fetchall()

            idx_raw, con_cols, con_rows, act_cols, act_row = [], None, None, None, None
            if verbose:
                cur.execute(_INDEXES_SQL, (name,))
                idx_raw = cur.fetchall()            # (index, role, scans, definition)
                cur.execute(_CONSTRAINTS_SQL, (name,))
                con_cols = [d.name for d in cur.description]
                con_rows = cur.fetchall()
                cur.execute(_TABLE_ACTIVITY_SQL, (name,))
                act_cols = [d.name for d in cur.description]
                act_row = cur.fetchone()
    except psycopg.Error as exc:
        _print_error(exc)
        return

    est = h["est_rows"]
    rows_txt = "row count unknown" if est is None or est < 0 else f"~{est:,} rows"
    title = f'{h["kind"].capitalize()} "{h["schema"]}.{h["name"]}"  —  owner {h["owner"]}, {rows_txt}'
    if verbose:
        title += f', {h["total_size"]} total ({h["table_size"]} data + {h["indexes_size"]} indexes)'

    block = _bold(_green(title)) + "\n\n"
    block += _format_section("Columns", col_cols, col_rows)

    if parent:
        block += _dim("Partition of ") + f"{parent[0]} {parent[1]}" + "\n\n"
    if partkey:
        block += _dim(f"Partitioned by {partkey}") + "\n"
        if partitions:
            shown = partitions[:20]
            block += render_table(
                ["partition", "bounds", "size"], [list(p) for p in shown],
                header_sgr=_header_sgr(),
            ) + "\n"
            if len(partitions) > len(shown):
                block += _dim(f"… and {len(partitions) - len(shown)} more partition(s)") + "\n"
            block += "\n"
        else:
            block += _dim("(no partitions yet)") + "\n\n"

    if verbose:
        total_scans = sum(r[2] for r in idx_raw)
        idx_rows = [
            [r[0], r[1], _index_usage_cell(r[2], total_scans, r[1]), r[3]] for r in idx_raw
        ]
        block += _format_section("Indexes", ["index", "role", "usage", "definition"], idx_rows)
        block += _format_section("Constraints", con_cols, con_rows)
        if idx_raw:
            block += _dim(
                "index usage = scans since the last stats reset · "
                "green frequent · yellow minor · red unused/rare"
            ) + "\n\n"
        if act_row is not None:
            block += _format_section("Activity", ["field", "value"],
                                     list(zip(act_cols, act_row, strict=False)))
    else:
        block += _dim(rf"(\d+ {name} for indexes, constraints, sizes & activity)") + "\n"

    _emit(block.rstrip("\n") + "\n", state, check_width=False)


# --- CREATE TABLE reconstruction (\dT) ------------------------------------
# All read-only. to_regclass() returns NULL (not an error) for a missing name.
_DDL_META_SQL = """\
SELECT c.oid,
       quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS qname,
       c.relkind,
       pg_catalog.pg_get_partkeydef(c.oid) AS partkey,  -- NULL unless partitioned parent
       (SELECT quote_ident(pn.nspname) || '.' || quote_ident(p.relname) || ' '
               || pg_catalog.pg_get_expr(c.relpartbound, c.oid)
        FROM pg_catalog.pg_inherits inh
        JOIN pg_catalog.pg_class p ON p.oid = inh.inhparent
        JOIN pg_catalog.pg_namespace pn ON pn.oid = p.relnamespace
        WHERE inh.inhrelid = c.oid) AS partition_of
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = pg_catalog.to_regclass(%s);"""

_DDL_COLUMNS_SQL = """\
SELECT quote_ident(a.attname),
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       a.attnotnull,
       pg_catalog.pg_get_expr(ad.adbin, ad.adrelid),
       a.attidentity, a.attgenerated
FROM pg_catalog.pg_attribute a
LEFT JOIN pg_catalog.pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;"""

_DDL_CONSTRAINTS_SQL = """\
SELECT quote_ident(conname), pg_catalog.pg_get_constraintdef(oid, true), contype
FROM pg_catalog.pg_constraint
WHERE conrelid = %s AND contype IN ('p', 'u', 'f', 'c', 'x')
ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'f' THEN 2
                      WHEN 'c' THEN 3 ELSE 4 END, conname;"""

# Indexes that don't back a constraint (PK/unique/exclusion are emitted as
# constraints above, so excluding them here avoids duplicates).
_DDL_INDEXES_SQL = """\
SELECT pg_catalog.pg_get_indexdef(i.indexrelid)
FROM pg_catalog.pg_index i
WHERE i.indrelid = %s
  AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conindid = i.indexrelid)
ORDER BY i.indexrelid::regclass::text;"""

_RELKIND_NAMES = {
    "v": "a view (try its SELECT with \\d+)", "m": "a materialized view",
    "S": "a sequence (try \\ds)", "c": "a composite type",
    "f": "a foreign table", "i": "an index", "t": "a TOAST table",
}


def _handle_table_ddl(session: DBSession, name: str, state: dict) -> None:
    """``\\dT <table>`` — print a reconstructed ``CREATE TABLE`` statement."""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_DDL_META_SQL, (name,))
            meta = cur.fetchone()
            if meta is None:
                print(_red(f'No relation named "{name}" in the current database ')
                      + _dim("(try schema.table).") + "\n")
                return
            oid, qname, relkind, partkey, partition_of = meta
            if relkind not in ("r", "p"):
                what = _RELKIND_NAMES.get(relkind, "not an ordinary table")
                print(_red(f"{qname} is {what}.") + "\n")
                return
            cur.execute(_DDL_COLUMNS_SQL, (oid,))
            columns = cur.fetchall()
            cur.execute(_DDL_CONSTRAINTS_SQL, (oid,))
            constraints = cur.fetchall()
            cur.execute(_DDL_INDEXES_SQL, (oid,))
            indexes = [r[0] for r in cur.fetchall()]
    except psycopg.Error as exc:
        _print_error(exc)
        return

    ddl = build_create_table(qname, columns, constraints, indexes,
                             partition_by=partkey or None)
    # The DDL itself is emitted plain (no colour) so it pastes cleanly.
    out = ddl + "\n"
    if partition_of:
        out += _dim(f"-- note: {qname} is a partition of {partition_of}") + "\n"
    out += _dim("-- structural DDL; no ownership/grants/storage params "
                "(serial columns need their sequence created separately)") + "\n"
    _emit(out, state, check_width=False)


_ROLE_FIELDS = (
    "name", "can_login", "superuser", "inherit", "createdb",
    "createrole", "replication", "bypassrls", "connlimit", "validuntil",
)


def _handle_roles(session: DBSession, args: list[str], state: dict) -> None:
    """``\\du`` — show roles/groups, or one role's detail with ``\\du <role>``."""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_ROLES_SQL)
            roles = [dict(zip(_ROLE_FIELDS, row, strict=False)) for row in cur.fetchall()]
            cur.execute(_ROLE_MEMBERS_SQL)
            members = [(g, m, bool(a)) for g, m, a in cur.fetchall()]
    except psycopg.Error as exc:
        _print_error(exc)
        return

    if args:
        block = render_role_detail(args[0], roles, members, _COLOR)
        if block is None:
            print(_red(f'No role named "{args[0]}".') + "\n")
            return
    else:
        block = render_roles(roles, members, _COLOR)
    _emit(block.rstrip("\n") + "\n", state, check_width=False)


def _privs_from_bools(bools) -> set:
    """Turn the seven has_table_privilege booleans into a privilege-name set."""
    return {name for name, flag in zip(TABLE_PRIVS, bools, strict=False) if flag}


def _handle_privileges(session: DBSession, args: list[str], state: dict) -> None:
    """``\\dp`` — object-access map, or detail for one table or role."""
    conn = session.conn
    try:
        if not args:
            block = _privilege_overview(conn)
        else:
            name = args[0]
            with conn.cursor() as cur:
                cur.execute(_PRIV_TABLE_META_SQL, (name,))
                meta = cur.fetchone()
            if meta is not None:
                block = _table_privileges(conn, name, meta)
            else:
                with conn.cursor() as cur:
                    cur.execute(_PRIV_ROLE_EXISTS_SQL, (name,))
                    role_row = cur.fetchone()
                if role_row is None:
                    print(_red(f'No table or role named "{name}".') + "\n")
                    return
                block = _role_privileges(conn, name, bool(role_row[0]))
    except psycopg.Error as exc:
        _print_error(exc)
        return
    # The overview is a genuinely wide matrix — let it scroll sideways in the
    # pager. The per-table / per-role detail views are narrow (height only).
    _emit(block.rstrip("\n") + "\n", state, check_width=not args)


def _privilege_overview(conn: psycopg.Connection) -> str:
    multi = _has_multiple_schemas(conn)
    schema_cells: dict[str, dict[str, set]] = {}
    schema_public: dict[str, set] = {}
    table_cells: dict[str, dict[str, set]] = {}
    table_public: dict[str, set] = {}
    with conn.cursor() as cur:
        cur.execute(_PRIV_SUPERUSERS_SQL)
        superusers = [r[0] for r in cur.fetchall()]

        cur.execute(_PRIV_SCHEMA_GRID_SQL)
        for nsp, role, usage, create in cur.fetchall():
            schema_cells.setdefault(nsp, {})  # ensure the schema appears as a row
            privs = {p for p, flag in (("USAGE", usage), ("CREATE", create)) if flag}
            if privs:
                schema_cells[nsp][role] = privs

        cur.execute(_PRIV_SCHEMA_PUBLIC_SQL)
        for nsp, priv in cur.fetchall():
            schema_public.setdefault(nsp, set()).add(priv)

        cur.execute(_PRIV_TABLE_GRID_SQL)
        for nsp, rel, role, *flags in cur.fetchall():
            name = f"{nsp}.{rel}" if multi else rel
            privs = _privs_from_bools(flags)
            cell = table_cells.setdefault(name, {})
            if privs:
                cell[role] = privs

        cur.execute(_PRIV_TABLE_PUBLIC_SQL)
        for nsp, rel, priv in cur.fetchall():
            name = f"{nsp}.{rel}" if multi else rel
            table_public.setdefault(name, set()).add(priv)

    return render_privilege_overview(
        schema_cells, schema_public, table_cells, table_public, superusers, _COLOR
    )


def _table_privileges(conn: psycopg.Connection, name: str, meta) -> str:
    nsp, rel, owner = meta
    effective: dict[str, set] = {}
    direct: dict[str, set] = {}
    public_privs: set = set()
    with conn.cursor() as cur:
        cur.execute(_PRIV_TABLE_EFFECTIVE_SQL, (name,) * 7)
        for role, *flags in cur.fetchall():
            effective[role] = _privs_from_bools(flags)
        cur.execute(_PRIV_TABLE_DIRECT_SQL, (name,))
        for grantee, priv in cur.fetchall():
            if grantee == "PUBLIC":
                public_privs.add(priv)
            else:
                direct.setdefault(grantee, set()).add(priv)
        cur.execute(_ROLE_MEMBERS_SQL)
        members = [(g, m, bool(a)) for g, m, a in cur.fetchall()]
        cur.execute(_PRIV_SUPERUSERS_SQL)
        superusers = [r[0] for r in cur.fetchall()]
    return render_table_privileges(
        nsp, rel, owner, effective, direct, public_privs, members, superusers, _COLOR
    )


def _role_privileges(conn: psycopg.Connection, name: str, is_super: bool) -> str:
    schemas: list[tuple[str, bool, bool]] = []
    tables: list[tuple[str, set]] = []
    if not is_super:
        with conn.cursor() as cur:
            cur.execute(_PRIV_ROLE_SCHEMAS_SQL, (name, name))
            schemas = [(nsp, bool(u), bool(c)) for nsp, u, c in cur.fetchall()]
            cur.execute(_PRIV_ROLE_TABLES_SQL, (name,) * 7)
            multi = len({nsp for nsp, _, _ in schemas}) > 1
            for nsp, rel, *flags in cur.fetchall():
                qname = f"{nsp}.{rel}" if multi else rel
                tables.append((qname, _privs_from_bools(flags)))
    return render_role_privileges(name, is_super, schemas, tables, _COLOR)


def _has_multiple_schemas(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_catalog.pg_namespace "
            "WHERE nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND nspname NOT LIKE 'pg\\_%';"
        )
        return cur.fetchone()[0] > 1


# Relation (table) locks in the current database, with who holds/waits on them
# and — for waiters — who's blocking. Our own backend and the system catalogs
# are left out so the view is about *your* tables.
_LOCKS_SQL = """\
SELECT n.nspname AS schema,
       c.relname AS table,
       l.mode,
       l.granted,
       l.pid,
       a.usename AS user,
       a.state,
       now() - COALESCE(a.xact_start, a.query_start) AS duration,
       pg_catalog.pg_blocking_pids(l.pid) AS blocked_by,
       a.query
FROM pg_catalog.pg_locks l
JOIN pg_catalog.pg_class c ON c.oid = l.relation
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'relation'
  AND l.pid <> pg_catalog.pg_backend_pid()
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY l.granted, n.nspname, c.relname, l.pid;"""


def _format_duration(td) -> str:
    if td is None:
        return "—"
    secs = max(0, int(td.total_seconds()))
    if secs < 60:
        return f"{td.total_seconds():.1f}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def _collapse(text: str, limit: int) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _locks_block(rows) -> str:
    """Render lock rows (schema, table, mode, granted, pid, user, state,
    duration, blocked_by, query) as a coloured table with a summary line."""
    table_rows = []
    waiting = 0
    for schema, tbl, mode, granted, pid, usr, st, dur, blockers, query in rows:
        name = tbl if schema == "public" else f"{schema}.{tbl}"
        if granted:
            status = _green("held")
        else:
            status = _red("waiting")
            waiting += 1
        blocked = _red(", ".join(map(str, blockers))) if blockers else "—"
        q = _collapse(query, 48) if query else "—"
        table_rows.append([
            name, mode.replace("Lock", ""), status, str(pid), usr or "—",
            st or "—", _format_duration(dur), blocked, q,
        ])
    header = ["table", "mode", "status", "pid", "user", "state", "duration", "blocked by", "query"]
    summary = f"{len(rows)} lock(s)" + (f", {_red(str(waiting) + ' waiting')}" if waiting else "")
    return render_table(header, table_rows, header_sgr=_header_sgr()) + "\n" + _dim(summary)


# Cluster parameters (GUCs). current_setting() gives the human-readable value
# ("128MB", not "16384"); source tells us which ones were changed from default.
_PARAM_SQL = """\
SELECT name,
       current_setting(name) AS value,
       source,
       context,
       pending_restart,
       short_desc
FROM pg_catalog.pg_settings
WHERE name ILIKE '%%' || %s || '%%'
ORDER BY name;"""

_SOURCE_SHORT = {
    "configuration file": "config file",
    "environment variable": "env",
    "command line": "command line",
}


def _params_block(rows) -> str:
    """Render setting rows (name, value, source, context, pending_restart,
    short_desc); non-default values are highlighted."""
    table_rows = []
    customized = 0
    for name, value, source, context, pending, desc in rows:
        if source == "default":
            shown = value
        else:
            shown = _green(value)  # changed from the built-in default
            customized += 1
        if pending:
            shown += _red(" (needs restart)")
        table_rows.append([
            name, shown, _SOURCE_SHORT.get(source, source), context,
            _collapse(desc or "", 44),
        ])
    header = ["parameter", "value", "source", "context", "description"]
    summary = f"{len(rows)} parameter(s)" + (f", {customized} non-default" if customized else "")
    return render_table(header, table_rows, header_sgr=_header_sgr()) + "\n" + _dim(summary)


def _handle_params(session: DBSession, args: list[str], state: dict) -> None:
    """``\\params [filter]`` — cluster parameters, filtered by name substring."""
    name_filter = args[0] if args else ""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_PARAM_SQL, (name_filter,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not rows:
        print(_red(f'No parameters matching "{name_filter}".') + "\n")
        return
    _emit(_params_block(rows) + "\n", state, check_width=True)


_EXPORT_FORMATS = ("csv", "tsv", "json", "jsonl")
# File extensions mapped to a format, for inferring it from the destination name.
_EXT_FORMAT = {
    ".csv": "csv", ".tsv": "tsv", ".tab": "tsv",
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
}


def _parse_export(arg: str) -> tuple[str, str | None, str]:
    """Split ``\\export`` input into (query, destination-or-None, format).

    Accepts ``(query)``, ``(query) to file.csv``, ``(query) file.csv`` and an
    optional trailing ``as <format>``; a trailing ``;`` is tolerated. The format
    is taken from ``as <format>`` if given, else the file extension, else csv.
    Without parentheses the whole thing is the query. Query is "" when absent.
    """
    arg = arg.strip()
    if arg.endswith(";"):
        arg = arg[:-1].strip()
    if not arg:
        return "", None, "csv"

    if arg.startswith("("):
        close = arg.rfind(")")  # last ')' — tolerates nested parens like count(*)
        if close == -1:
            return "", None, "csv"
        query = arg[1:close].strip()
        rest = arg[close + 1:].strip()
    else:
        query, rest = arg, ""

    fmt_override = None
    m = re.search(r"\bas\s+(\w+)\s*$", rest, re.IGNORECASE)  # trailing "as <fmt>"
    if m:
        fmt_override = m.group(1).lower()
        rest = rest[: m.start()].strip()
    if rest.lower().startswith("to "):
        rest = rest[3:].strip()
    dest = os.path.expanduser(rest.strip().strip("'\"")) if rest else None

    if query.endswith(";"):
        query = query[:-1].strip()

    if fmt_override:
        fmt = fmt_override
    elif dest:
        fmt = _EXT_FORMAT.get(os.path.splitext(dest)[1].lower(), "csv")
    else:
        fmt = "csv"
    return query, dest, fmt


def _handle_export(session: DBSession, arg: str, state: dict) -> None:
    """``\\export (query) [to file] [as csv|tsv|json]`` — results to a file."""
    query, dest, fmt = _parse_export(arg)
    if not query:
        print(_red(r"usage: \export (select …) [to file] [as csv|tsv|json]") + "\n")
        return
    if fmt not in _EXPORT_FORMATS:
        print(_red(f'Unknown format "{fmt}".') + _dim(" Use one of: csv, tsv, json, jsonl.") + "\n")
        return
    if dest is None:
        dest = f"pysql_export_{time.strftime('%Y%m%d_%H%M%S')}.{fmt}"
    dest = os.path.abspath(dest)

    # Stream to a temp file, then atomically move into place, so a failed export
    # never leaves a half-written (or empty) file behind.
    folder = os.path.dirname(dest) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    except OSError as exc:
        print(_red(f"Could not write to {folder}: ") + str(exc) + "\n")
        return
    exporter = {"json": _export_json, "jsonl": _export_jsonl}.get(fmt, _export_copy)
    try:
        with os.fdopen(fd, "wb") as f:
            count = exporter(session, query, fmt, f)
        os.replace(tmp, dest)
    except psycopg.Error as exc:
        _safe_unlink(tmp)
        _print_error(exc)
        return
    except OSError as exc:
        _safe_unlink(tmp)
        print(_red(f"Could not write {dest}: ") + str(exc) + "\n")
        return
    noun = "row" if count == 1 else "rows"
    print(_green(f"Exported {count:,} {noun}") + _dim(f" ({fmt}) to {dest}") + "\n")


def _export_copy(session: DBSession, query: str, fmt: str, f) -> int:
    """csv/tsv via a streaming server-side COPY. Returns the row count."""
    delimiter = r", DELIMITER E'\t'" if fmt == "tsv" else ""
    copy_sql = f"COPY ({query}) TO STDOUT WITH (FORMAT csv, HEADER{delimiter})"
    with session.conn.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            for block in copy:
                f.write(block)
        return cur.rowcount


def _export_json(session: DBSession, query: str, fmt: str, f) -> int:
    """A JSON array of row objects (via json_agg). Returns the row count."""
    with session.conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*)::int, coalesce(json_agg(t), '[]')::text FROM ({query}) t"
        )
        count, text = cur.fetchone()
    f.write(text.encode("utf-8"))
    f.write(b"\n")
    return count


def _export_jsonl(session: DBSession, query: str, fmt: str, f) -> int:
    """JSON Lines — one row object per line, streamed. Returns the row count."""
    count = 0
    with session.conn.cursor() as cur:
        for (row_json,) in cur.stream(f"SELECT row_to_json(t)::text FROM ({query}) t"):
            f.write(row_json.encode("utf-8"))
            f.write(b"\n")
            count += 1
    return count


def _table_ident(table: str) -> sql.Identifier:
    """A safely-quoted table identifier, splitting ``schema.table`` if present."""
    return sql.Identifier(*table.split("."))


def _has_sql(text: str) -> bool:
    """True if ``text`` holds anything but whitespace and comments."""
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    without_line = re.sub(r"--[^\n]*", "", without_block)
    return bool(without_line.strip())


def _handle_cat(arg: str, state: dict) -> None:
    """``\\cat <file>`` — print a file from the filesystem (paged if it's large)."""
    path = os.path.expanduser(arg.strip().strip("'\""))
    if not path:
        print(_red(r"usage: \cat <file>") + "\n")
        return
    if os.path.isdir(path):
        print(_red(f"{path} is a directory.") + "\n")
        return
    try:
        # errors="replace": a stray non-UTF-8 byte shows as � rather than crashing.
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(_red(f"File not found: {path}") + "\n")
        return
    except OSError as exc:
        print(_red(f"Could not read {path}: ") + str(exc) + "\n")
        return
    if content == "":
        print(_dim(f"({path} is empty)") + "\n")
        return
    # Strip only the trailing newline(s): _emit prints with its own newline, so
    # the file lands on screen exactly once without a blank line tacked on.
    _emit(content.rstrip("\n"), state)


# Trailing " <interval> [count]" on a \watch line: two optional numbers at the
# very end. A query that must itself end in a number should be terminated with
# ';' so the ';' separates it from these args (as in `\watch select 1; 5`).
_WATCH_ARGS_RE = re.compile(r"\s+(\d+(?:\.\d+)?)(?:\s+(\d+))?\s*$")


def _handle_watch(session: DBSession, raw: str, state: dict) -> None:
    """``\\watch <query> [interval [count]]`` — re-run a query on a timer.

    ``interval`` is seconds (default 2, decimals allowed); ``count`` is how many
    runs (default: until Ctrl-C). Example: ``\\watch select now(); 5 5`` runs every
    5 seconds, 5 times.
    """
    raw = raw.strip()
    interval, count = 2.0, 0  # 0 count = run until interrupted
    m = _WATCH_ARGS_RE.search(raw)
    if m:
        interval = float(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 0
        query = raw[: m.start()].strip()
    else:
        query = raw
    if not query:
        print(_red(r"usage: \watch <query> [interval_seconds [count]]")
              + _dim("   e.g. \\watch select now(); 5 5") + "\n")
        return
    if interval <= 0:
        print(_red("watch interval must be greater than 0 seconds.") + "\n")
        return

    statements, remainder = split_statements(query)
    if _has_sql(remainder):
        statements.append(remainder.strip())
    if not statements:
        print(_red("No statement to watch.") + "\n")
        return

    # Paging a live loop would be unusable, so force it off for the duration.
    watch_state = {**state, "pager": False}
    plan = f"every {interval:g}s" + (f", {count} run(s)" if count else ", until Ctrl-C")
    print(_dim(f"\\watch — {plan}.") + "\n")
    runs = 0
    try:
        while True:
            runs += 1
            tag = f"#{runs}" + (f"/{count}" if count else "")
            header = f"── {tag}  {time.strftime('%H:%M:%S')} "
            print(_c(header, "36") + _dim("─" * max(0, 40 - len(header))))
            for stmt in statements:
                _run_statement(session.conn, stmt, watch_state["timing"], watch_state)
            if count and runs >= count:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n" + _dim(f"\\watch stopped after {runs} run(s).") + "\n")
        return
    print(_dim(f"\\watch done — {runs} run(s).") + "\n")


def _handle_broadcast(manager: SessionManager, raw: str, state: dict) -> None:
    """``\\run-on-active <query>`` — run a query on every open session.

    Each open connection is a target: everything ``\\C`` has opened, plus the
    startup session. By default pysql asks before running on each one; turn that
    confirmation off with ``\\prompt`` and it runs on all of them without asking.
    At a prompt, ``a`` runs on all the remaining sessions without further asking
    and ``q`` stops; a blank line (the default) skips the session.
    """
    query = raw.strip()
    if not query:
        print(_red(r"usage: \run-on-active <query>")
              + _dim("   runs it on every open session; "
                     r"\prompt toggles the per-database confirmation") + "\n")
        return

    statements, remainder = split_statements(query)
    if _has_sql(remainder):
        statements.append(remainder.strip())
    if not statements:
        print(_red("No statement to run.") + "\n")
        return

    targets = list(manager.sessions.items())
    confirm = state.get("confirm_broadcast", True)
    note = "" if confirm else "  (\\prompt is off — no confirmation)"
    print(_dim(f"\\run-on-active — {len(targets)} open session(s){note}") + "\n")

    ran = 0
    for name, sess in targets:
        info = sess.conn.info
        where = f"{info.user}@{info.host or 'local'}/{info.dbname}"
        if confirm:
            answer = _ask(_bold(f"Run on {name}") + _dim(f"  ({where})") + " [y/N/a/q]: ")
            choice = (answer or "").lower()
            if answer is None or choice == "q":
                print(_dim(f"  (stopped — ran on {ran} session(s))") + "\n")
                return
            if choice == "a":
                confirm = False  # run this one and every remaining one without asking
            elif choice not in ("y", "yes"):
                print(_dim(f"  · skipped {name}"))
                continue
        print(_c(f"── {name} ", "36") + _dim(f"({where})"))
        for stmt in statements:
            _run_statement(sess.conn, stmt, state["timing"], state)
        ran += 1
    print(_dim(f"\\run-on-active done — ran on {ran} session(s).") + "\n")


def _handle_saved(manager: SessionManager, raw: str, state: dict) -> None:
    """``\\s`` — list saved queries; ``\\s <name>`` run one; ``\\s <name> <query>`` save.

    A one-token argument is a name to **run**; two or more tokens save the rest of
    the line as the query under that name. Query text may hold ``{bind}``
    placeholders (filled in interactively when the query is run).
    """
    raw = raw.strip()
    if not raw:
        _list_saved_queries()
        return
    parts = raw.split(None, 1)
    if len(parts) == 1:
        _run_saved_query(manager, parts[0], state)
    else:
        _save_query_cmd(parts[0], parts[1])


def _list_saved_queries() -> None:
    queries = load_queries()
    if not queries:
        print(_dim(f"No saved queries yet — save one with \\s <name> <query>  ({QUERIES_PATH}).")
              + "\n")
        return
    width = max(len(n) for n in queries)
    lines = [_dim(f"Saved queries ({QUERIES_PATH}):")]
    for name in sorted(queries):
        preview = " ".join(queries[name].split())
        if len(preview) > 56:
            preview = preview[:55] + "…"
        binds = extract_placeholders(queries[name])
        tag = _c("  binds: " + ", ".join("{" + b + "}" for b in binds), "36") if binds else ""
        lines.append(f"  {_c(name.ljust(width), '35')}  {_dim(preview)}{tag}")
    lines.append(_dim("Run one with \\s <name>; delete with \\sD <name>."))
    print("\n".join(lines) + "\n")


def _save_query_cmd(name: str, query: str) -> None:
    if not valid_name(name):
        print(_red("Query name must be a single token of letters, digits, '.', '-' or '_'.")
              + _dim(r"   (\s <name> <query>)") + "\n")
        return
    query = query.strip()
    if not query:
        print(_red(r"usage: \s <name> <query>") + "\n")
        return
    existed = name in load_queries()
    try:
        save_query(name, query)
    except OSError as exc:
        print(_red(f"Could not write {QUERIES_PATH}: ") + str(exc) + "\n")
        return
    binds = extract_placeholders(query)
    msg = _green(f"{'Updated' if existed else 'Saved'} query {name}")
    if binds:
        msg += _dim("  (binds: " + ", ".join("{" + b + "}" for b in binds) + ")")
    print(msg + _dim(f"\n  run it with \\s {name}") + "\n")


def _run_saved_query(manager: SessionManager, name: str, state: dict) -> None:
    """Stage a saved query on the prompt (filling any binds first) for you to run.

    It doesn't execute on its own — the filled-in query is dropped onto the next
    prompt so you can review or edit it and press Enter (or Ctrl-C to discard).
    """
    queries = load_queries()
    if name not in queries:
        print(_red(f'No saved query named "{name}".') + _dim(r"   (\s lists them)") + "\n")
        return
    template = queries[name]
    binds = extract_placeholders(template)
    if binds:
        values = prompt_for_binds(binds, title=f"Binds for \\s {name}", query=template)
        if values is None:
            print(_dim("  (cancelled)") + "\n")
            return
        query = substitute(template, values)
    else:
        query = template
    # Hand it to the REPL to pre-fill the prompt (like Ctrl-R history), rather
    # than running it — so a destructive query is never fired without a look.
    state["prefill"] = query.strip()


def _handle_saved_delete(raw: str) -> None:
    """``\\sD <name>`` — delete a saved query."""
    name = raw.strip().split(None, 1)[0] if raw.strip() else ""
    if not name:
        print(_red(r"usage: \sD <name>") + _dim(r"   (\s lists saved queries)") + "\n")
        return
    try:
        deleted = delete_query(name)
    except OSError as exc:
        print(_red(f"Could not write {QUERIES_PATH}: ") + str(exc) + "\n")
        return
    if deleted:
        print(_green(f"Deleted saved query {name}.") + "\n")
    else:
        print(_red(f'No saved query named "{name}".') + _dim(r"   (\s lists them)") + "\n")


def _handle_file(session: DBSession, arg: str, state: dict) -> None:
    """``\\f <file> [stop]`` (``\\file``) — run the SQL statements in a file.

    With a trailing ``stop`` it halts at the first error instead of running on.
    """
    arg = arg.strip()
    stop_on_error = False
    m = re.search(r"\s+stop\s*$", arg, re.IGNORECASE)  # trailing "stop" keyword
    if m:
        stop_on_error = True
        arg = arg[: m.start()].strip()
    path = os.path.expanduser(arg.strip("'\""))
    if not path:
        print(_red(r"usage: \f <file> [stop]") + "\n")
        return
    if not os.path.isfile(path):
        print(_red(f"File not found: {path}") + "\n")
        return
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(_red(f"Could not read {path}: ") + str(exc) + "\n")
        return

    # Same quote/dollar-tag-aware splitter the REPL uses; also run a final
    # statement that wasn't terminated with a semicolon.
    statements, remainder = split_statements(content)
    if _has_sql(remainder):
        statements.append(remainder.strip())
    if not statements:
        print(_dim(f"No statements in {path}.") + "\n")
        return

    failed = ran = 0
    for stmt in statements:
        ran += 1
        if not _run_statement(session.conn, stmt, state["timing"], state):
            failed += 1
            if stop_on_error:
                break
    total = len(statements)
    if failed and stop_on_error:
        print(_red(f"Stopped on error after {ran} of {total} statement(s) from {path}") + "\n")
    elif failed:
        print(_red(f"{total} statement(s) from {path} — {failed} failed") + "\n")
    else:
        print(_green(f"{total} statement(s) from {path}") + "\n")


def _parse_import(arg: str) -> tuple[str, str | None, str]:
    """Split ``\\import`` input into (file, table-or-None, format).

    Grammar: ``<file> [to <table>] [as <format>]``. The file may be quoted (for
    spaces). Format comes from ``as <format>`` if given, else the file extension,
    else csv.
    """
    arg = arg.strip()
    if arg.endswith(";"):
        arg = arg[:-1].strip()

    fmt_override = None
    m = re.search(r"\bas\s+(\w+)\s*$", arg, re.IGNORECASE)
    if m:
        fmt_override = m.group(1).lower()
        arg = arg[: m.start()].strip()

    rest = ""
    if arg[:1] in ("'", '"'):  # quoted file path
        end = arg.find(arg[0], 1)
        file = arg[1:end] if end != -1 else arg[1:]
        rest = arg[end + 1:].strip() if end != -1 else ""
    else:
        m2 = re.search(r"\s+to\s+", arg, re.IGNORECASE)
        if m2:
            file, rest = arg[: m2.start()].strip(), "to " + arg[m2.end():].strip()
        else:
            file = arg.strip()

    table = rest[3:].strip().strip("'\"") if rest.lower().startswith("to ") else None
    file = os.path.expanduser(file)

    if fmt_override:
        fmt = fmt_override
    elif file:
        fmt = _EXT_FORMAT.get(os.path.splitext(file)[1].lower(), "csv")
    else:
        fmt = "csv"
    return file, table, fmt


def _handle_import(session: DBSession, arg: str, state: dict) -> None:
    """``\\import <file> [to <table>] [as csv|tsv|json|jsonl]`` — load a file."""
    file, table, fmt = _parse_import(arg)
    if not file:
        print(_red(r"usage: \import <file> [to <table>] [as csv|tsv|json|jsonl]") + "\n")
        return
    if fmt not in _EXPORT_FORMATS:
        print(_red(f'Unknown format "{fmt}".') + _dim(" Use one of: csv, tsv, json, jsonl.") + "\n")
        return
    if not os.path.isfile(file):
        print(_red(f"File not found: {file}") + "\n")
        return
    if table is None:  # infer the table name from the file name
        table = os.path.splitext(os.path.basename(file))[0]

    importer = {"json": _import_json, "jsonl": _import_json}.get(fmt, _import_copy)
    try:
        count = importer(session, file, table, fmt)
    except (psycopg.Error, OSError, ValueError) as exc:
        if isinstance(exc, psycopg.Error):
            _print_error(exc)
        else:
            print(_red(f"Import failed: {exc}") + "\n")
        return
    noun = "row" if count == 1 else "rows"
    print(_green(f"Imported {count:,} {noun}") + _dim(f" ({fmt}) into {table}") + "\n")


def _import_copy(session: DBSession, file: str, table: str, fmt: str) -> int:
    """csv/tsv via streaming COPY FROM STDIN; columns matched by header name."""
    delimiter = "\t" if fmt == "tsv" else ","
    with open(file, newline="", encoding="utf-8") as fh:
        header = next(_csv.reader(fh, delimiter=delimiter), None)
    if not header:
        raise ValueError("file has no header row")
    columns = [c.strip() for c in header]

    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER{})").format(
        _table_ident(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", DELIMITER E'\\t'") if fmt == "tsv" else sql.SQL(""),
    )
    with session.conn.cursor() as cur:
        with cur.copy(copy_sql) as copy, open(file, "rb") as fh:
            while block := fh.read(65536):
                copy.write(block)
        return cur.rowcount


def _import_json(session: DBSession, file: str, table: str, fmt: str) -> int:
    """json array / jsonl: top-level keys become columns; inserted atomically."""
    rows: list[dict] = []
    if fmt == "jsonl":
        with open(file, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    else:
        with open(file, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data if isinstance(data, list) else [data]
    if not rows:
        return 0

    columns = list(rows[0].keys())
    insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        _table_ident(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )

    def adapt(value):  # nested json goes into json/jsonb columns
        return Json(value) if isinstance(value, (dict, list)) else value

    params = [[adapt(row.get(c)) for c in columns] for row in rows]
    # all-or-nothing, even under autocommit
    with session.conn.transaction(), session.conn.cursor() as cur:
        cur.executemany(insert, params)
    return len(rows)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# A "top" for the server: client backends and what they're doing right now.
# Duration is query runtime for active backends, else time in the current state.
_TOP_SQL = """\
SELECT pid, usename, datname, state, wait_event_type, wait_event,
       now() - CASE WHEN state = 'active' THEN query_start ELSE state_change END AS duration,
       query
FROM pg_catalog.pg_stat_activity
WHERE backend_type = 'client backend'
  AND pid <> pg_catalog.pg_backend_pid()
ORDER BY (state = 'active') DESC, duration DESC NULLS LAST;"""

_STATE_SHORT = {
    "idle in transaction": "idle in txn",
    "idle in transaction (aborted)": "idle in txn (aborted)",
    "fastpath function call": "fastpath",
}


def _top_block(rows, max_conn) -> str:
    """Render a pg_stat_activity snapshot: a summary line plus busy backends."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[3]] = counts.get(r[3], 0) + 1
    active = counts.get("active", 0)
    idle = counts.get("idle", 0)
    in_txn = counts.get("idle in transaction", 0) + counts.get("idle in transaction (aborted)", 0)

    parts = [f"{len(rows)} connection(s)", _green(f"{active} active"), f"{idle} idle"]
    if in_txn:
        parts.append(_c(f"{in_txn} idle in txn", "33"))
    summary = " · ".join(parts)
    if max_conn:
        summary += _dim(f"  (max {max_conn})")

    busy = [r for r in rows if r[3] != "idle"]
    if not busy:
        return summary + "\n" + _dim("No active backends.")

    table_rows = []
    for pid, user, db, st, wait_type, wait, dur, query in busy:
        label = _STATE_SHORT.get(st, st or "—")
        if st == "active":
            label = _green(label)
        elif st and "aborted" in st:
            label = _red(label)
        elif st and "transaction" in st:
            label = _c(label, "33")
        waiting = f"{wait_type}:{wait}" if wait else "—"
        if wait_type == "Lock":
            waiting = _red(waiting)
        table_rows.append([
            str(pid), user or "—", db or "—", label,
            _format_duration(dur), waiting, _collapse(query, 50) if query else "—",
        ])
    header = ["pid", "user", "db", "state", "duration", "wait", "query"]
    return summary + "\n\n" + render_table(header, table_rows, header_sgr=_header_sgr())


_SIGNAL_LOOKUP_SQL = """\
SELECT usename, datname, state, query
FROM pg_catalog.pg_stat_activity WHERE pid = %s;"""


def _confirm(question: str) -> bool:
    """Ask a yes/no question on the terminal; default (and on EOF/Ctrl-C) is no."""
    try:
        return input(question).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _handle_signal(session: DBSession, args: list[str], *, terminate: bool) -> None:
    """``\\cancel <pid>`` / ``\\kill <pid>`` — cancel a query or end a backend."""
    verb = "kill" if terminate else "cancel"
    if not args:
        print(_red(f"usage: \\{verb} <pid>") + "\n")
        return
    try:
        pid = int(args[0])
    except ValueError:
        print(_red(f"pid must be a number (got {args[0]!r}).") + "\n")
        return

    if pid == session.conn.info.backend_pid:
        print(_red("That's this pysql session — use \\q to quit.") + "\n")
        return

    try:
        with session.conn.cursor() as cur:
            cur.execute(_SIGNAL_LOOKUP_SQL, (pid,))
            row = cur.fetchone()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if row is None:
        print(_red(f"No backend with pid {pid}.") + "\n")
        return
    usename, datname, st, query = row

    print(_dim("Target: ") + f"pid {pid} · {usename or '?'}@{datname or '?'} · {st or '?'}")
    if query and query != "<insufficient privilege>":
        print(_dim("Query:  ") + _collapse(query, 70))
    if not terminate and st != "active":
        print(_c(f"Note: backend is {st}, not running a query — "
                 r"\cancel won't do much; \kill ends the session.", "33"))

    action = "Terminate this backend" if terminate else "Cancel this query"
    if not _confirm(action + "? [y/N] "):
        print(_dim("Aborted.") + "\n")
        return

    fn = "pg_terminate_backend" if terminate else "pg_cancel_backend"
    try:
        with session.conn.cursor() as cur:
            cur.execute(f"SELECT pg_catalog.{fn}(%s)", (pid,))
            ok = cur.fetchone()[0]
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if ok:
        done = f"Terminated backend {pid}." if terminate else f"Cancel signal sent to backend {pid}."
        print(_green(done) + "\n")
    else:
        print(_red(f"Could not signal backend {pid} — it may have already ended.") + "\n")


# User schemas (system ones hidden, like psql's \dn), with an object count and
# a flag for the current default schema.
_SCHEMAS_SQL = """\
SELECT n.nspname,
       pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
       (SELECT count(*) FROM pg_catalog.pg_class c
        WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'v', 'm', 'f')) AS objects,
       n.nspname = current_schema() AS is_current
FROM pg_catalog.pg_namespace n
WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
ORDER BY n.nspname;"""


def _schemas_block(rows, search_path) -> str:
    """Render schema rows (name, owner, objects, is_current); mark the current."""
    table_rows = []
    for nsp, owner, objects, is_current in rows:
        marker = "→" if is_current else " "
        name = _green(nsp) if is_current else nsp
        table_rows.append([marker, name, owner or "—", str(objects)])
    out = render_table(["", "schema", "owner", "objects"], table_rows, header_sgr=_header_sgr())
    if search_path:
        out = _dim("search_path: " + ", ".join(search_path)) + "\n" + out
    return out


# --- sequences (\ds) ------------------------------------------------------
# All reads below are non-advancing: pg_sequences and `SELECT ... FROM <seq>`
# read the on-disk state only; nextval/currval/setval are never used, so a \ds
# never changes a sequence.
_SEQ_LIST_SQL = """\
SELECT schemaname            AS schema,
       sequencename          AS name,
       data_type::text       AS type,
       sequenceowner         AS owner,
       last_value            AS last_value
FROM pg_catalog.pg_sequences
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY schemaname, sequencename;"""

# Fuzzy fallback: sequences whose (optionally schema-qualified) name matches.
_SEQ_FUZZY_SQL = """\
SELECT schemaname      AS schema,
       sequencename    AS name,
       data_type::text AS type,
       sequenceowner   AS owner,
       last_value      AS last_value
FROM pg_catalog.pg_sequences
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND (sequencename ILIKE %s OR (schemaname || '.' || sequencename) ILIKE %s)
ORDER BY schemaname, sequencename;"""

# Full state+config for one sequence, matched by name (and optional schema).
_SEQ_MATCH_SQL = """\
SELECT schemaname, sequencename, sequenceowner, data_type::text,
       start_value, min_value, max_value, increment_by, cycle,
       cache_size, last_value
FROM pg_catalog.pg_sequences
WHERE sequencename = %s AND (%s::text IS NULL OR schemaname = %s)
ORDER BY schemaname;"""

# Resolve a sequence (by name, optional schema) to the bits \ds <name> <value>
# needs: its increment, range, and oid (passed to setval).
_SEQ_RESOLVE_SQL = """\
SELECT schemaname, sequencename, increment_by, min_value, max_value,
       -- the percents are doubled so psycopg doesn't read them as placeholders
       format('%%I.%%I', schemaname, sequencename)::regclass::oid AS oid
FROM pg_catalog.pg_sequences
WHERE sequencename = %s AND (%s::text IS NULL OR schemaname = %s)
ORDER BY schemaname;"""

# The table column a sequence backs (SERIAL / IDENTITY), if any.
_SEQ_OWNED_SQL = """\
SELECT quote_ident(tn.nspname) || '.' || quote_ident(tc.relname) || '.' || a.attname
FROM pg_catalog.pg_class sc
JOIN pg_catalog.pg_namespace sn ON sn.oid = sc.relnamespace
JOIN pg_catalog.pg_depend dep ON dep.objid = sc.oid
     AND dep.classid = 'pg_catalog.pg_class'::regclass AND dep.deptype IN ('a', 'i')
JOIN pg_catalog.pg_class tc ON tc.oid = dep.refobjid
JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = dep.refobjid AND a.attnum = dep.refobjsubid
WHERE sc.relname = %s AND sn.nspname = %s AND sc.relkind = 'S'
LIMIT 1;"""


def _sequence_list_block(rows) -> str:
    """Render sequence rows (schema, name, type, owner, last_value)."""
    table_rows = [[sch, name, typ, owner or "—", last] for sch, name, typ, owner, last in rows]
    return render_table(["schema", "name", "type", "owner", "last value"], table_rows,
                        header_sgr=_header_sgr())


def _show_sequence_detail(session: DBSession, row, state: dict) -> None:
    """Render one sequence's config and current value as a field/value table.

    Read-only: the sequence is never advanced. ``is_called`` (and the next value
    it would hand out) come from ``SELECT ... FROM <seq>``, which only reads.
    """
    (schema, name, owner, dtype, start, minimum, maximum,
     increment, cycle, cache, last_value) = row

    # Authoritative current state, read straight from the sequence (non-advancing
    # — it reads the relation, it does not call nextval). The relation's
    # last_value is populated even before the first nextval, whereas
    # pg_sequences.last_value stays NULL until then, so this reads friendlier.
    # Best-effort: may be denied by privileges, in which case we keep the
    # pg_sequences value and simply omit is_called / next value.
    is_called = None
    try:
        with session.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(
                sql.Identifier(schema, name)))
            last_value, is_called = cur.fetchone()
    except psycopg.Error:
        pass

    owned_by = ""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_SEQ_OWNED_SQL, (name, schema))
            got = cur.fetchone()
            owned_by = got[0] if got else ""
    except psycopg.Error:
        pass

    pairs: list[tuple[str, object]] = [
        ("schema", schema),
        ("name", name),
        ("owner", owner),
        ("data type", dtype),
        ("start", start),
        ("minimum", minimum),
        ("maximum", maximum),
        ("increment", increment),
        ("cycles?", cycle),
        ("cache", cache),
        ("last value", last_value),
    ]
    if is_called is not None:
        pairs.append(("is_called", is_called))
        # What the next nextval() would return (typical monotonic case). We do
        # NOT call it; this is arithmetic on the values we just read.
        if last_value is not None:
            nxt = last_value + increment if is_called else last_value
            pairs.append(("next value", nxt))
    if owned_by:
        pairs.append(("owned by", owned_by))

    title = _bold(f"sequence {schema}.{name}") + "\n"
    _emit(title + render_table(["field", "value"], pairs, header_sgr=_header_sgr()) + "\n",
          state, check_width=False)


def _set_sequence(session: DBSession, name_arg: str, value_arg: str) -> None:
    """``\\ds <name> <value>`` — set a sequence's value with ``setval`` (mutating).

    Unlike viewing, this *changes* the sequence, so it asks for confirmation and
    reports the value the next ``nextval()`` will return. Setting below the
    current maximum of a key column can cause later duplicate-key errors, which
    is exactly why it confirms.
    """
    try:
        value = int(value_arg)
    except ValueError:
        print(_red(f'Not an integer: "{value_arg}"')
              + _dim(r"  (usage: \ds <name> <value>)") + "\n")
        return

    schema, dot, bare = name_arg.rpartition(".")
    schema = schema or None
    name = bare if dot else name_arg
    try:
        with session.conn.cursor() as cur:
            cur.execute(_SEQ_RESOLVE_SQL, (name, schema, schema))
            matches = cur.fetchall()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not matches:
        print(_red(f'No sequence named "{name_arg}".') + "\n")
        return
    if len(matches) > 1:
        print(_red(f'Several sequences named "{name}" — qualify with schema.name.') + "\n")
        return

    sch, nm, increment, minv, maxv, oid = matches[0]
    if value < minv or value > maxv:
        print(_red(f"{value} is out of range for {sch}.{nm} ")
              + _dim(f"(min {minv}, max {maxv})") + "\n")
        return

    current = None  # current value for the before→after prompt (safe read)
    try:
        with session.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT last_value FROM {}").format(sql.Identifier(sch, nm)))
            current = cur.fetchone()[0]
    except psycopg.Error:
        pass

    # setval(seq, value) sets is_called=true, so the next nextval() is value+increment.
    nxt = value + increment
    before = f"{current} → " if current is not None else ""
    if not _confirm(
        _red("Set ") + f"{sch}.{nm}" + _red(" to ") + f"{value}"
        + _dim(f"  ({before}next nextval() → {nxt})") + "  [y/N] "
    ):
        print(_dim("Cancelled — sequence unchanged.") + "\n")
        return
    try:
        with session.conn.cursor() as cur:
            cur.execute("SELECT setval(%s::regclass, %s)", (oid, value))
    except psycopg.Error as exc:
        _print_error(exc)
        return
    print(_green(f"Sequence {sch}.{nm} set to {value}.")
          + _dim(f"  (next nextval() → {nxt})") + "\n")


def _handle_sequences(session: DBSession, args: list[str], state: dict) -> None:
    """``\\ds`` lists; ``\\ds <name>`` views (read-only); ``\\ds <name> <value>`` sets."""
    if len(args) > 2:
        print(_red(r"usage: \ds [<name> [<value>]]") + "\n")
        return
    if len(args) == 2:
        _set_sequence(session, args[0], args[1])
        return
    if not args:
        try:
            with session.conn.cursor() as cur:
                cur.execute(_SEQ_LIST_SQL)
                rows = cur.fetchall()
        except psycopg.Error as exc:
            _print_error(exc)
            return
        if not rows:
            print(_dim("No sequences.") + "\n")
            return
        _emit(_sequence_list_block(rows) + "\n", state, check_width=True)
        return

    arg = args[0]
    schema, dot, bare = arg.rpartition(".")
    schema = schema or None  # 'public.s' -> ('public','s'); 's' -> (None,'s')
    name = bare if dot else arg
    try:
        with session.conn.cursor() as cur:
            cur.execute(_SEQ_MATCH_SQL, (name, schema, schema))
            matches = cur.fetchall()
    except psycopg.Error as exc:
        _print_error(exc)
        return

    if len(matches) == 1:
        _show_sequence_detail(session, matches[0], state)
        return
    if len(matches) > 1:
        # Same bare name in several schemas — show them so the user can qualify.
        print(_dim(f'Several sequences named "{name}" — qualify with schema.name:'))
        rows = [(m[0], m[1], m[3], m[2], m[10]) for m in matches]  # schema,name,type,owner,last
        _emit(_sequence_list_block(rows) + "\n", state, check_width=True)
        return

    # No exact match: treat the argument as a fuzzy filter so you can find one.
    try:
        with session.conn.cursor() as cur:
            cur.execute(_SEQ_FUZZY_SQL, (f"%{arg}%", f"%{arg}%"))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not rows:
        print(_red(f'No sequence matching "{arg}".') + "\n")
        return
    print(_dim(f'No exact match; sequences matching "{arg}":'))
    _emit(_sequence_list_block(rows) + "\n", state, check_width=True)


def _handle_schemas(session: DBSession, state: dict) -> None:
    """``\\dn`` — list schemas; the current (default) schema is marked."""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_SCHEMAS_SQL)
            rows = cur.fetchall()
            cur.execute("SELECT current_schemas(false)")
            search_path = cur.fetchone()[0]
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not rows:
        print(_dim("No user schemas.") + "\n")
        return
    _emit(_schemas_block(rows, search_path) + "\n", state, check_width=False)


def _handle_kill(session: DBSession, args: list[str]) -> None:
    """``\\kill <pid|user>`` — terminate one backend (numeric) or all of a user's."""
    if not args:
        print(_red(r"usage: \kill <pid>  or  \kill <user>") + "\n")
        return
    if args[0].isdigit():
        _handle_signal(session, args, terminate=True)
    else:
        _handle_kill_bulk(session, args[0], by="user")


def _handle_kill_bulk(session: DBSession, target: str, *, by: str) -> None:
    """Terminate every client backend for a user (``by='user'``) or database."""
    column = "usename" if by == "user" else "datname"
    label = "user" if by == "user" else "database"
    # No 'client backend' filter: a non-superuser sees other users' backend_type
    # as NULL, and those are exactly the ones \kill <user> targets. Dropping only
    # autovacuum (IS DISTINCT FROM keeps the NULL/hidden rows).
    lookup = (
        f"SELECT pid, usename, datname, state, query FROM pg_catalog.pg_stat_activity "
        f"WHERE {column} = %s AND backend_type IS DISTINCT FROM 'autovacuum worker' "
        f"AND pid <> pg_catalog.pg_backend_pid() ORDER BY pid"
    )
    try:
        with session.conn.cursor() as cur:
            cur.execute(lookup, (target,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not rows:
        print(_red(f'No other backends for {label} "{target}".') + "\n")
        return

    shown = rows[:25]
    preview = [
        [str(p), u or "—", d or "—", st or "—", _collapse(q, 40) if q else "—"]
        for p, u, d, st, q in shown
    ]
    print(render_table(["pid", "user", "db", "state", "query"], preview,
                       header_sgr=_header_sgr()))
    if len(rows) > len(shown):
        print(_dim(f"… and {len(rows) - len(shown)} more"))

    if not _confirm(f'Terminate {len(rows)} backend(s) for {label} "{target}"? [y/N] '):
        print(_dim("Aborted.") + "\n")
        return

    killed = 0
    for pid, *_ in rows:
        try:
            with session.conn.cursor() as cur:
                cur.execute("SELECT pg_catalog.pg_terminate_backend(%s)", (pid,))
                if cur.fetchone()[0]:
                    killed += 1
        except psycopg.Error as exc:
            print(_red(f"pid {pid}: ") + str(exc).strip())
    print(_green(f"Terminated {killed} of {len(rows)} backend(s).") + "\n")


def _heat_cell(value: int, peak: int) -> str:
    """One heatmap cell: dim dot for zero, else green→yellow→red by intensity."""
    if peak <= 0 or value <= 0:
        return _dim("·")
    frac = value / peak
    code = "32" if frac <= 1 / 3 else "33" if frac <= 2 / 3 else "31"
    return _c("█", code)


# Metric rows shown in the heatmap: (label, index into each history sample).
_HEAT_METRICS = (("active", 0), ("blocked", 1), ("idle-txn", 2))


def _heatmap_block(history, width: int = 60) -> str:
    """Stacked heat rows (active / blocked / idle-in-txn), intensity over time.

    Each row is coloured relative to its own peak in the window, so bursts stand
    out; the current and peak values are printed alongside.
    """
    if len(history) < 2:
        return _dim("heatmap: collecting samples…")
    lines = []
    for label, i in _HEAT_METRICS:
        vals = [h[i] for h in history][-width:]
        peak = max(vals)
        cells = "".join(_heat_cell(v, peak) for v in vals)
        lines.append(_dim(f"{label:>8} ") + cells + _dim(f"  now {vals[-1]}, peak {peak}"))
    legend = _dim("          intensity ") + _green("low") + " " + _c("busy", "33") + " " + _red("hot")
    return "\n".join(lines) + "\n" + legend


def _top_monitor(session: DBSession, refresh: float, *, input=None, output=None) -> None:
    """Full-screen auto-refreshing ``\\top``; ``q``/Esc/Ctrl-C quits."""
    history: deque = deque(maxlen=120)
    data = {"rows": [], "max_conn": None, "err": None, "stamp": "", "t": 0.0}

    def fetch() -> None:
        try:
            with session.conn.cursor() as cur:
                cur.execute(_TOP_SQL)
                rows = cur.fetchall()
                cur.execute("SELECT current_setting('max_connections')::int")
                data["max_conn"] = cur.fetchone()[0]
            data["rows"], data["err"] = rows, None
            data["stamp"] = time.strftime("%H:%M:%S")
            # (active, blocked = active+waiting, idle-in-transaction)
            active = sum(1 for r in rows if r[3] == "active")
            blocked = sum(1 for r in rows if r[3] == "active" and r[5])
            idle_txn = sum(1 for r in rows if r[3] and r[3].startswith("idle in transaction"))
            history.append((active, blocked, idle_txn))
        except Exception as exc:  # keep the UI alive; show the error
            data["err"] = str(exc).strip()

    def render():
        now = time.monotonic()
        if data["t"] == 0.0 or now - data["t"] >= refresh * 0.5:
            data["t"] = now
            fetch()
        if data["err"]:
            return ANSI(_red("Error: " + data["err"]) + _dim("\n\nq to quit"))
        header = _dim(f"pysql top · {data['stamp']} · every {refresh:g}s · q to quit")
        return ANSI(header + "\n\n" + _heatmap_block(history) + "\n\n"
                    + _top_block(data["rows"], data["max_conn"]))

    kb = KeyBindings()

    @kb.add("q")
    @kb.add("c-c")
    @kb.add("escape")
    def _(event):
        event.app.exit()

    app: Application = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render))])),
        key_bindings=kb,
        full_screen=True,
        refresh_interval=refresh,
        input=input,
        output=output,
    )
    app.run()


def _handle_top(session: DBSession, state: dict, args: list[str] | None = None) -> None:
    """``\\top [secs]`` — a snapshot, or a live monitor refreshing every N seconds."""
    if args:
        try:
            refresh = float(args[0])
        except ValueError:
            print(_red(f"refresh must be a number of seconds (got {args[0]!r}).") + "\n")
            return
        if refresh <= 0:
            print(_red("refresh must be greater than 0.") + "\n")
            return
        if sys.stdout.isatty():
            _top_monitor(session, max(0.5, refresh))
            return
        print(_dim("(not a terminal — showing a single snapshot)") + "\n")

    try:
        with session.conn.cursor() as cur:
            cur.execute(_TOP_SQL)
            rows = cur.fetchall()
            cur.execute("SELECT current_setting('max_connections')::int")
            max_conn = cur.fetchone()[0]
    except psycopg.Error as exc:
        _print_error(exc)
        return
    _emit(_top_block(rows, max_conn) + "\n", state, check_width=True)


def _handle_locks(session: DBSession, state: dict) -> None:
    """``\\locks`` — current table locks, holders, and blocking relationships."""
    try:
        with session.conn.cursor() as cur:
            cur.execute(_LOCKS_SQL)
            rows = cur.fetchall()
    except psycopg.Error as exc:
        _print_error(exc)
        return
    if not rows:
        print(_green("No locks held on user tables.") + "\n")
        return
    _emit(_locks_block(rows) + "\n", state, check_width=True)


# Grouped meta-command reference for \?. Left column is the bare command; the
# description carries the argument forms (variants separated by " · ").
_HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Inspect schema", [
        (r"\d", r"list tables · \d <table> columns + partitions · \d+ <table> full detail"),
        (r"\dn", "list schemas (current one marked →)"),
        (r"\dT", r"\dT <table> — print a reconstructed CREATE TABLE statement"),
        (r"\ds", r"list sequences · \ds <name> view (read-only) · \ds <name> <n> set"),
        (r"\dx", "list installed extensions"),
        (r"\l", r"list databases · \l <db> stats for one"),
        (r"\du", r"list roles & groups · \du <role> details one"),
        (r"\dp", r"object-access map · \dp <table|role> details one"),
    ]),
    ("Activity & locks", [
        (r"\locks", "current table locks, holders, and blockers"),
        (r"\top", r"backend activity · \top <secs> live monitor w/ heatmap"),
        (r"\cancel", r"\cancel <pid> — cancel a running query"),
        (r"\kill", r"\kill <pid|user> end a backend · \killdb <db> all on a database"),
        (r"\params", r"cluster parameters · \params <filter> by name"),
    ]),
    ("Files & I/O", [
        (r"\export", r"\export (select …) [to file] [as csv|tsv|json|jsonl]"),
        (r"\import", r"\import <file> [to table] [as csv|tsv|json|jsonl]"),
        (r"\f", r"\f <file> run a SQL file (stop = halt on first error; also \file)"),
        (r"\cat", r"\cat <file> print a file from disk (Tab-completes paths)"),
        (r"\watch", r"\watch <query> [interval [count]] — re-run a query on a timer"),
        (r"\s", r"\s list saved queries · \s <name> run one · \s <name> <query> save"),
        (r"\sD", r"\sD <name> delete a saved query"),
    ]),
    ("Connections & profiles", [
        (r"\c", r"\c <db> connect to another database (\c - = previous)"),
        (r"\C", r"list profiles · \C <name> opens one (\C - = previous)"),
        (r"\add", r"\add <name> save the current connection as a profile"),
        (r"\close", "close the current profile session"),
        (r"\w", r"toggle reader/writer endpoint (needs a reader: host; * = on writer)"),
        (r"\P", r"\P <port> pin this session's SSM tunnel to a fixed local port (0 = auto)"),
        (r"\run-on-active", r"\run-on-active <query> run it on every open session (asks first)"),
        (r"\enc", r"\enc <pw> encrypt a password for a profile file"),
        (r"\conninfo", "show the current connection"),
    ]),
    ("Settings & session", [
        (r"\timing", "toggle query timing"),
        (r"\pager", r"result paging on/off · \pager on|off"),
        (r"\prompt", r"confirm each \run-on-active target on/off · \prompt on|off"),
        (r"\color", r"colour output on/off · \color on|off"),
        (r"\v", "toggle Vi edit mode (:w runs, :q clears; set vi=on in ~/.pysqlrc)"),
        (r"\save", "save current settings to ~/.pysqlrc"),
        (r"\q", "quit"),
        ("Ctrl-R", "fuzzy-search command history · PgUp/PgDn scroll it"),
    ]),
]


def _help_text() -> str:
    """Render the grouped \\? reference with aligned, colour-coded columns."""
    width = max(len(cmd) for _, rows in _HELP_SECTIONS for cmd, _ in rows)
    lines = [_bold("Meta-commands") + _dim("  — type a command without a trailing ;")]
    for title, rows in _HELP_SECTIONS:
        lines.append("")
        lines.append(_bold(_green(title)))
        for cmd, desc in rows:
            lines.append(f"  {_c(cmd.ljust(width), '36')}  {desc}")
    return "\n".join(lines) + "\n"


def _handle_meta(command: str, manager: SessionManager, state: dict) -> bool:
    """Handle a backslash meta-command. Returns False to signal quit."""
    stripped = command.strip()

    # \enc takes the password verbatim (it may contain spaces) and must not be
    # split like other commands. Its history entry is dropped elsewhere.
    if stripped == r"\enc" or stripped.startswith("\\enc "):
        password = stripped[len(r"\enc"):].strip()
        if not password:
            print(_red(r"usage: \enc <password>") + "\n")
            return True
        print(crypto.encrypt(password))
        print(_dim(f"use this as the profile's password in {active_profile_path()}") + "\n")
        return True

    session = manager.session

    # \export takes a raw query (with its own spaces, quotes and parens), so it
    # must not be whitespace-split like the other commands.
    if stripped == r"\export" or stripped.startswith("\\export "):
        _handle_export(session, stripped[len(r"\export"):].strip(), state)
        return True
    if stripped == r"\import" or stripped.startswith("\\import "):
        _handle_import(session, stripped[len(r"\import"):].strip(), state)
        return True
    for alias in (r"\file", r"\f"):  # \file checked first (\\f is its prefix)
        if stripped == alias or stripped.startswith(alias + " "):
            _handle_file(session, stripped[len(alias):].strip(), state)
            return True
    # \cat takes a raw path (handled before the split so paths with spaces work,
    # and ahead of \c in the dispatch below).
    if stripped == r"\cat" or stripped.startswith("\\cat "):
        _handle_cat(stripped[len(r"\cat"):].strip(), state)
        return True
    # \watch takes a raw query plus trailing interval/count, so it must not be
    # whitespace-split like the plain commands below.
    if stripped == r"\watch" or stripped.startswith("\\watch "):
        _handle_watch(session, stripped[len(r"\watch"):].strip(), state)
        return True
    # \run-on-active takes a raw query to run across every open session, so it
    # must not be whitespace-split like the plain commands below.
    if stripped == r"\run-on-active" or stripped.startswith("\\run-on-active "):
        _handle_broadcast(manager, stripped[len(r"\run-on-active"):].strip(), state)
        return True
    # \sD (delete) before \s (list/run/save); both take raw text (a query may
    # contain spaces/quotes), so they're handled before the whitespace split.
    if stripped == r"\sD" or stripped.startswith("\\sD "):
        _handle_saved_delete(stripped[len(r"\sD"):].strip())
        return True
    if stripped == r"\s" or stripped.startswith("\\s "):
        _handle_saved(manager, stripped[len(r"\s"):].strip(), state)
        return True

    parts = stripped.split()
    cmd = parts[0] if parts else ""
    args = parts[1:]
    if cmd in (r"\q", r"\quit"):
        return False
    elif cmd == r"\C":
        _connect_profile(manager, args)
    elif cmd == r"\P":
        _handle_rebind_port(manager, args)
    elif cmd == r"\w":
        _handle_toggle_rw(manager)
    elif cmd == r"\add":
        _add_profile(manager, args)
    elif cmd == r"\close":
        try:
            now = manager.close_current()
        except ValueError as exc:
            print(_red(str(exc)) + "\n")
            return True
        _announce_session(manager, now)
    elif cmd == r"\l":
        if args:
            _show_detail(session.conn, _DB_STAT_SQL, args[0],
                         missing=f'No database named "{args[0]}".', state=state)
        else:
            _run_statement(session.conn, _DB_LIST_SQL, False, state)
    elif cmd == r"\d":
        if args:
            _describe_table(session, args[0], state)
        else:
            _handle_table_list(session, state)
    elif cmd == r"\d+":
        if args:
            _describe_table(session, args[0], state, verbose=True)
        else:
            _handle_table_list(session, state)
    elif cmd == r"\dn":
        _handle_schemas(session, state)
    elif cmd == r"\dT":
        if args:
            _handle_table_ddl(session, args[0], state)
        else:
            print(_red(r"usage: \dT <table>") + "\n")
    elif cmd == r"\ds":
        _handle_sequences(session, args, state)
    elif cmd == r"\dx":
        _run_statement(session.conn, _EXTENSIONS_SQL, False, state)
    elif cmd == r"\du":
        _handle_roles(session, args, state)
    elif cmd == r"\dp":
        _handle_privileges(session, args, state)
    elif cmd == r"\locks":
        _handle_locks(session, state)
    elif cmd == r"\top":
        _handle_top(session, state, args)
    elif cmd == r"\cancel":
        _handle_signal(session, args, terminate=False)
    elif cmd == r"\kill":
        _handle_kill(session, args)
    elif cmd == r"\killdb":
        if args:
            _handle_kill_bulk(session, args[0], by="database")
        else:
            print(_red(r"usage: \killdb <database>") + "\n")
    elif cmd == r"\params":
        _handle_params(session, args, state)
    elif cmd == r"\c":
        _connect_meta(session, args)
    elif cmd == r"\conninfo":
        info = session.conn.info
        role = ""
        if session.profile is not None and session.profile.reader and session.mode:
            endpoint = _endpoint_host(session.profile, session.mode)
            tag = _red("writer") if session.mode == "writer" else _green("reader")
            role = f", on the {tag} endpoint ({endpoint})"
        print(
            f"Database {_green(info.dbname)}, user {_green(info.user)}, "
            f"host {info.host or 'local'}, port {info.port}{role}\n"
        )
    elif cmd == r"\v":
        state["vi"] = not state["vi"]
        if state["vi"]:
            print(_green("Vi mode on.") + _dim("  Esc → NORMAL; w/e/b/dw/… navigate "
                  "& edit; :w runs, :q clears; PgUp/PgDn scroll history.") + "\n")
        else:
            print("Vi mode off.\n")
    elif cmd == r"\timing":
        state["timing"] = not state["timing"]
        print(f"Timing is {'on' if state['timing'] else 'off'}.\n")
    elif cmd == r"\pager":
        arg = args[0].lower() if args else ""
        if arg == "on":
            state["pager"] = True
        elif arg == "off":
            state["pager"] = False
        elif arg:
            print(_red(r"usage: \pager on|off") + "\n")
            return True
        print(f"Pager is {'on' if state['pager'] else 'off'}.\n")
    elif cmd == r"\prompt":
        arg = args[0].lower() if args else ""
        if arg == "on":
            state["confirm_broadcast"] = True
        elif arg == "off":
            state["confirm_broadcast"] = False
        elif arg:
            print(_red(r"usage: \prompt on|off")
                  + _dim("  (or just \\prompt to toggle)") + "\n")
            return True
        else:
            state["confirm_broadcast"] = not state["confirm_broadcast"]
        on = state["confirm_broadcast"]
        print(f"\\run-on-active confirmation is {'on' if on else 'off'}"
              + ("." if on else _dim(" — it will run on every open session without asking.")) + "\n")
    elif cmd == r"\color":
        arg = args[0].lower() if args else ""
        if arg == "on":
            _set_color(True)
        elif arg == "off":
            _set_color(False)
        elif arg:
            print(_red(r"usage: \color on|off") + _dim("  (or just \\color to toggle)") + "\n")
            return True
        else:
            _set_color(not _COLOR)
        print(f"Colour is {'on' if _COLOR else 'off'}.\n")
    elif cmd == r"\save":
        saved = {
            "pager": state["pager"], "timing": state["timing"],
            "vi": state["vi"], "confirm_broadcast": state["confirm_broadcast"],
            "display_mode": "color" if _COLOR else "plain",
        }
        try:
            save_config(saved)
        except OSError as exc:
            print(_red(f"Could not write {CONFIG_PATH}: ") + str(exc) + "\n")
            return True
        on = lambda v: "on" if v else "off"  # noqa: E731
        print(_green(f"Saved settings to {CONFIG_PATH}")
              + _dim(f"  (pager {on(saved['pager'])}, timing {on(saved['timing'])}, "
                     f"vi {on(saved['vi'])}, prompt {on(saved['confirm_broadcast'])}, "
                     f"display_mode {saved['display_mode']})") + "\n")
    elif cmd in (r"\?", r"\h", r"\help"):
        print(_help_text())
    else:
        print(_red(f"Unknown command: {cmd}") + _dim(r"  (try \?)") + "\n")
    return True


class _HistorySearch:
    """Sentinel returned from the prompt when Ctrl-R asks for the fuzzy finder."""

    def __init__(self, query: str) -> None:
        self.query = query


def _build_keybindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-r")  # replace readline's (reverse-i-search) with our fuzzy finder
    def _(event):
        event.app.exit(result=_HistorySearch(event.current_buffer.text))

    # Page-up / page-down scroll through command history (Up/Down do too).
    @kb.add("pageup")
    def _(event):
        event.current_buffer.history_backward()

    @kb.add("pagedown")
    def _(event):
        event.current_buffer.history_forward()

    # Vi NORMAL-mode ":" commands. These are "blind" — prompt_toolkit has no
    # echoed command line, so nothing shows as you type them (the mode tag on the
    # right tells you you're in NORMAL mode). ":w"/":wq" run the line like Enter;
    # ":q" clears it like Ctrl-C. Quitting pysql itself is still \q / Ctrl-D.
    @kb.add(":", "w", "enter", filter=vi_navigation_mode)
    @kb.add(":", "w", "q", "enter", filter=vi_navigation_mode)
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add(":", "q", "enter", filter=vi_navigation_mode)
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    return kb


_VI_MODE_LABELS = {
    InputMode.INSERT: "INSERT",
    InputMode.INSERT_MULTIPLE: "INSERT",
    InputMode.REPLACE: "REPLACE",
    InputMode.REPLACE_SINGLE: "REPLACE",
    InputMode.NAVIGATION: "NORMAL",
}


def _vi_mode_indicator():
    """Right-prompt tag showing the Vi input mode — empty unless Vi mode is on."""
    app = get_app()
    if app.editing_mode != EditingMode.VI:
        return ""
    mode = app.vi_state.input_mode
    label = _VI_MODE_LABELS.get(mode, "NORMAL")
    # A selection in NORMAL mode means visual (character/line) selection.
    if mode == InputMode.NAVIGATION and app.current_buffer.selection_state is not None:
        label = "VISUAL"
    return FormattedText([("reverse", f" -- {label} -- ")])


class _SafePasswordHistory(FileHistory):
    """FileHistory that never records a ``\\enc`` line (it holds a plaintext pw)."""

    def append_string(self, string: str) -> None:
        if string.strip().startswith(r"\enc"):
            return
        super().append_string(string)


def _prompt_prefix(manager: SessionManager) -> str:
    """The coloured prompt prefix: the profile name (if any) plus the database."""
    info = manager.session.conn.info
    db = _dim("(") + _c(info.dbname, "36") + _dim(") ")
    if manager.current in manager.profiles:
        return _c(manager.current, "35") + " " + db
    return db


def _writer_marker(manager: SessionManager) -> str:
    """A red ``*`` when the session is on its writer endpoint (and a reader exists),
    so it's obvious you're pointed at the primary. Empty otherwise."""
    session = manager.session
    profile = getattr(session, "profile", None)
    if profile is not None and profile.reader and session.mode == "writer":
        return _red("*")
    return ""


def repl(manager: SessionManager) -> None:
    completer = SQLCompleter(manager.session)
    prompt_session: PromptSession = PromptSession(
        history=_SafePasswordHistory(str(HISTORY_FILE)),
        completer=completer,
        complete_while_typing=False,  # Tab to complete, like psql
        key_bindings=_build_keybindings(),
        rprompt=_vi_mode_indicator,  # shows "-- NORMAL/INSERT --" when Vi mode is on
    )
    # Built-in defaults, overridden by ~/.pysqlrc [settings].
    settings = load_config()
    _apply_display_settings(settings)  # colour on/off + zebra band colours
    set_keepalive_defaults(  # global SSM tunnel keep-alive defaults (profile wins)
        settings.get("keepalive"), settings.get("keepalive_interval")
    )
    state = {
        "timing": settings.get("timing", True),
        "pager": settings.get("pager", True),
        "vi": settings.get("vi", False),
        "confirm_broadcast": settings.get("confirm_broadcast", True),
    }
    buffer = ""
    prefill = ""  # text to pre-load into the next prompt (from history search)

    print(_banner(manager.session))

    while True:
        is_continuation = bool(buffer.strip())
        # The current session can change under us via \C; re-read it each turn
        # so completion and the prompt follow the active profile.
        completer.session = manager.session
        completer.pending_text = buffer
        prefix = _prompt_prefix(manager)
        prompt = ANSI(prefix + (_dim("  ...> ") if is_continuation
                                else f"pysql{_writer_marker(manager)}> "))
        editing_mode = EditingMode.VI if state["vi"] else EditingMode.EMACS
        try:
            with patch_stdout():
                line = prompt_session.prompt(
                    prompt, default=prefill, editing_mode=editing_mode
                )
        except KeyboardInterrupt:
            # Ctrl-C clears the in-progress statement, like psql.
            buffer = ""
            prefill = ""
            continue
        except EOFError:
            # Ctrl-D quits.
            print()
            break

        # Ctrl-R opened the fuzzy history finder: run it, then re-prompt with
        # the chosen command pre-filled (or restore what was being typed).
        if isinstance(line, _HistorySearch):
            picked = fuzzy_history_search(prompt_session.history.get_strings(), line.query)
            prefill = picked if picked is not None else line.query
            continue
        prefill = ""

        # Meta-commands only apply when not mid-statement.
        if not buffer.strip() and line.strip().startswith("\\"):
            if not _handle_meta(line.strip(), manager, state):
                break
            # A meta-command may stage text for the prompt (e.g. \s drops a saved
            # query here instead of running it), like Ctrl-R history recall.
            prefill = state.pop("prefill", "")
            continue

        buffer += line + "\n"

        # Run every complete statement in the buffer; keep the trailing
        # unterminated remainder (verbatim) to continue on the next line.
        statements, buffer = split_statements(buffer)
        for statement in statements:
            _run_statement(manager.session.conn, statement, state["timing"], state)


def _print_usage() -> None:
    print(
        "pysql — a friendlier PostgreSQL client\n\n"
        "usage:\n"
        "  pysql [CONNINFO|URL]      connect using a libpq conninfo string or URL\n"
        "  pysql @PROFILE            connect using a saved profile\n"
        "  pysql --enc [PASSWORD]    print an encrypted token for a profile password\n"
        "  pysql --help              show this help\n\n"
        "With no argument, pysql uses libpq defaults (PGHOST, PGDATABASE, PGUSER, … / .pgpass)."
    )


def _run_enc(argv: list[str]) -> int:
    """``pysql --enc [password]`` — print a \\enc token without opening a session.

    The password may be passed on the command line (``--enc pw`` or ``--enc=pw``)
    or, if omitted, is read from a hidden prompt — which also keeps it out of your
    shell history and the process list. The token goes to stdout (so it can be
    captured/piped); the hint goes to stderr.
    """
    if argv[0].startswith("--enc="):
        password = argv[0][len("--enc="):]
    elif len(argv) > 1:
        password = argv[1]
    else:
        try:
            password = getpass.getpass("Password to encrypt: ")
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 1
    if not password:
        print(_red("usage: pysql --enc <password>")
              + _dim("   (or run `pysql --enc` with no argument to be prompted)"),
              file=sys.stderr)
        return 1
    print(crypto.encrypt(password))  # token to stdout — capturable
    print(_dim(f"use this as the profile's password in {active_profile_path()}"),
          file=sys.stderr)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        _print_usage()
        return 0
    if argv and (argv[0] == "--enc" or argv[0].startswith("--enc=")):
        return _run_enc(argv)
    arg = argv[0] if argv else ""
    try:
        if arg.startswith("@"):
            # `pysql @profile` launches straight into a saved profile.
            name = arg[1:]
            profiles = load_profiles()
            if name not in profiles:
                print(_red(f'No profile named "{name}" in {active_profile_path()}.'), file=sys.stderr)
                return 1
            session, err = _connect_profile_session(name, profiles[name])
            if err is not None:
                raise err  # formatted by the handlers below
            manager = SessionManager(session, name, is_profile=True)
        else:
            # A conninfo string or URL as the first argument, else libpq env
            # defaults (PGHOST, PGDATABASE, PGUSER, ... and ~/.pgpass).
            conninfo = arg or os.environ.get("DATABASE_URL", "")
            # autocommit (set in DBSession): each statement stands alone, so one
            # failure doesn't poison the session.
            session = DBSession(conninfo)
            manager = SessionManager(session, session.conn.info.dbname or "startup")
    except (crypto.CryptoError, rds.RdsAuthError, TunnelError) as exc:
        print(_red("Profile error: ") + str(exc), file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(_red("Could not connect: ") + str(exc).strip(), file=sys.stderr)
        return 1

    try:
        repl(manager)
    finally:
        manager.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
