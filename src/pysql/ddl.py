"""Reconstruct a ``CREATE TABLE`` statement from catalog data (``\\dT``).

Postgres exposes ``pg_get_viewdef`` / ``pg_get_constraintdef`` /
``pg_get_indexdef`` but has no single "table DDL" function, so we assemble one:
the columns (type, identity, generated, default, ``NOT NULL``), then the table
constraints (primary key / unique / foreign key / check, via
``pg_get_constraintdef``), then any remaining indexes (via ``pg_get_indexdef``).

It is a *structural* reconstruction, not a full ``pg_dump``: it leaves out
ownership, grants, storage parameters, triggers, comments and — for old-style
``serial`` columns — the separate ``CREATE SEQUENCE`` (the ``DEFAULT
nextval(...)`` is still shown). Identity columns are self-contained.

Pure: it takes already-fetched, already-quoted catalog data and returns text,
so it is trivial to test without a database.
"""

from __future__ import annotations

from collections.abc import Sequence

# A column, as fetched: (name, type, not_null, default, identity, generated).
# ``name``/``type``/``default`` are already server-quoted; ``identity`` is
# '' | 'a' | 'd'; ``generated`` is '' | 's'.
Column = tuple[str, str, bool, str | None, str, str]
# A constraint, as fetched: (quoted name, pg_get_constraintdef, contype).
Constraint = tuple[str, str, str]


def _column_line(name: str, type_: str, not_null: bool,
                 default: str | None, identity: str, generated: str) -> str:
    parts = [name, type_]
    if identity in ("a", "d"):
        # Identity implies NOT NULL; Postgres doesn't print it, so neither do we.
        parts.append("GENERATED %s AS IDENTITY"
                     % ("ALWAYS" if identity == "a" else "BY DEFAULT"))
    elif generated == "s" and default is not None:
        parts.append(f"GENERATED ALWAYS AS ({default}) STORED")
    elif default is not None:
        parts.append(f"DEFAULT {default}")
    if not_null and identity not in ("a", "d"):
        parts.append("NOT NULL")
    return " ".join(parts)


def build_create_table(
    qname: str,
    columns: Sequence[Column],
    constraints: Sequence[Constraint],
    indexes: Sequence[str],
    partition_by: str | None = None,
) -> str:
    """Return a ``CREATE TABLE`` statement (plus any ``CREATE INDEX`` lines).

    ``qname`` is the server-quoted ``schema.table``. ``indexes`` are full
    ``pg_get_indexdef`` strings (those backing a constraint are already
    excluded). ``partition_by`` is a ``pg_get_partkeydef`` body (e.g.
    ``RANGE (created_on)``) for a partitioned parent, else ``None``.
    """
    body = [_column_line(*col) for col in columns]
    body += [f"CONSTRAINT {name} {definition}" for name, definition, _ in constraints]
    inner = ",\n".join("    " + b for b in body)

    stmt = f"CREATE TABLE {qname} (\n{inner}\n)"
    if partition_by:
        stmt += f"\nPARTITION BY {partition_by}"
    stmt += ";"

    for idx in indexes:
        stmt += "\n" + idx + ";"
    return stmt
