"""Human-friendly object-privilege display for ``\\dp``.

psql's ``\\z`` / ``\\dp`` print the raw ACL (``alice=arwdDxt/owner``) and, worse,
only the *direct* grants — so they can't answer the question people actually
have: "can this user read that table?" when the answer runs through a group.

We lean on Postgres's own ``has_table_privilege()`` / ``has_schema_privilege()``
to get **effective** access (membership, inheritance, PUBLIC and ownership all
resolved correctly), then present it three ways:

* an **access map** (schemas and tables × roles) for the whole schema,
* a **per-table** breakdown with privileges spelled out and a ``via`` column
  that says *how* each role's access arrives (owner / direct / via a group),
* a **per-role** view of everything one role can reach.

Superusers bypass every privilege check, so they'd be "ALL" on everything; we
leave them out of the grids and note them instead.

Rendering only — it takes already-fetched data, so it tests without a database.
"""

from __future__ import annotations

from .table import render_table

# Table-level privileges, in a stable display order, with the single-letter
# codes used in the compact matrix (spelled out in the detail views).
TABLE_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
_LETTER = {
    "SELECT": "S", "INSERT": "I", "UPDATE": "U", "DELETE": "D",
    "TRUNCATE": "T", "REFERENCES": "R", "TRIGGER": "G",
}
_TABLE_LEGEND = "S select · I insert · U update · D delete · T truncate · R references · G trigger"
_SCHEMA_LEGEND = "U usage · C create"


def _codes(privs: set[str]) -> str:
    """Compact matrix cell: ALL / — / letter codes."""
    if not privs:
        return "—"
    if all(p in privs for p in TABLE_PRIVS):
        return "ALL"
    return "".join(_LETTER[p] for p in TABLE_PRIVS if p in privs)


def _spell(privs: set[str]) -> str:
    """Detail cell: ALL / — / full words in canonical order."""
    if not privs:
        return "—"
    if all(p in privs for p in TABLE_PRIVS):
        return "ALL"
    return ", ".join(p for p in TABLE_PRIVS if p in privs)


def _schema_codes(usage: bool, create: bool) -> str:
    return ("U" if usage else "") + ("C" if create else "") or "—"


def _schema_set_codes(privs: set[str]) -> str:
    return ("U" if "USAGE" in privs else "") + ("C" if "CREATE" in privs else "") or "—"


def _mk_color(color: bool):
    def c(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    return c


def _groups_of(role: str, members: list[tuple[str, str, bool]]) -> set[str]:
    """All roles ``role`` is a member of, transitively."""
    out: set[str] = set()
    stack = [role]
    while stack:
        cur = stack.pop()
        for grp, mem, _ in members:
            if mem == cur and grp not in out:
                out.add(grp)
                stack.append(grp)
    return out


# --- overview -------------------------------------------------------------

def render_privilege_overview(
    schema_cells: dict[str, dict[str, set]],
    schema_public: dict[str, set],
    table_cells: dict[str, dict[str, set]],
    table_public: dict[str, set],
    superusers: list[str],
    color: bool,
) -> str:
    """The access map: schema grid + table grid, sharing role columns.

    Cells show each role's access *beyond* the PUBLIC baseline (kept as its own
    column). A role whose only access is the universal PUBLIC grant therefore
    gets no column — otherwise, e.g. PG's public-schema USAGE would list every
    role in the database.
    """
    c = _mk_color(color)
    dim = lambda t: c(t, "2")  # noqa: E731
    hdr = "1" if color else None  # bold column headers

    # Columns: roles with any access beyond PUBLIC, in either grid.
    roles: set[str] = set()
    for obj, cells in schema_cells.items():
        pub = schema_public.get(obj, set())
        roles |= {r for r, s in cells.items() if s - pub}
    for obj, cells in table_cells.items():
        pub = table_public.get(obj, set())
        roles |= {r for r, s in cells.items() if s - pub}
    roles = sorted(roles)
    columns = ["", *roles, "PUBLIC"]

    out = c("Access map", "1;32") + "\n\n"

    # Schema grid.
    srows = []
    for schema in sorted(schema_cells):
        cells = schema_cells[schema]
        pub = schema_public.get(schema, set())
        row = [schema]
        row += [_schema_set_codes(cells.get(r, set()) - pub) for r in roles]
        row.append(_schema_set_codes(pub))
        srows.append(row)
    if srows:
        out += dim("Schemas") + "\n"
        out += render_table(columns, srows, header_sgr=hdr) + "\n"
        out += dim(_SCHEMA_LEGEND) + "\n\n"

    # Table grid.
    trows = []
    for name in sorted(table_cells):
        cells = table_cells[name]
        pub = table_public.get(name, set())
        row = [name]
        row += [_codes(cells.get(r, set()) - pub) for r in roles]
        row.append(_codes(pub))
        trows.append(row)
    if trows:
        out += dim("Tables") + "\n"
        out += render_table(columns, trows, header_sgr=hdr) + "\n"
        out += dim(_TABLE_LEGEND) + "\n"

    out += dim("\nrole columns show access beyond PUBLIC (shown as its own column)")
    if superusers:
        out += "\n" + dim("superusers (full access, not shown): ") + ", ".join(sorted(superusers))
    return out


# --- per-table ------------------------------------------------------------

def render_table_privileges(
    nsp: str,
    rel: str,
    owner: str,
    effective: dict[str, set],
    direct: dict[str, set],
    public_privs: set,
    members: list[tuple[str, str, bool]],
    superusers: list[str],
    color: bool,
) -> str:
    """Who can do what on one table, and how their access arrives."""
    c = _mk_color(color)
    dim = lambda t: c(t, "2")  # noqa: E731
    hdr = "1" if color else None

    rows = []
    for role in sorted(effective):
        eff = effective[role]
        if not eff:
            continue
        sources: list[str] = []
        explained: set[str] = set(public_privs)
        if role == owner:
            sources.append("owner")
            explained |= set(TABLE_PRIVS)
        if direct.get(role):
            sources.append("direct")
            explained |= direct[role]
        if eff - explained:  # access it has that direct/owner/PUBLIC don't explain
            granting = [
                g for g in sorted(_groups_of(role, members))
                if direct.get(g) or g == owner
            ]
            sources.append("via " + ", ".join(granting) if granting else "via group")
        rows.append([role, _spell(eff), " + ".join(sources) or "—"])

    if public_privs:
        rows.append(["PUBLIC", _spell(public_privs), "—"])

    title = c(f'Privileges on "{nsp}.{rel}"', "1;32") + dim(f"  —  owner {owner}")
    body = render_table(["role", "privileges", "via"], rows, header_sgr=hdr) if rows else dim("No grants.")
    out = title + "\n\n" + body
    if superusers:
        out += "\n" + dim("superusers (full access): ") + ", ".join(sorted(superusers))
    return out


# --- per-role -------------------------------------------------------------

def render_role_privileges(
    role: str,
    is_super: bool,
    schemas: list[tuple[str, bool, bool]],
    tables: list[tuple[str, set]],
    color: bool,
) -> str:
    """Everything one role can reach: schema usage + table privileges."""
    c = _mk_color(color)
    dim = lambda t: c(t, "2")  # noqa: E731
    hdr = "1" if color else None

    title = c(f'What "{role}" can access', "1;32")
    if is_super:
        return title + "\n\n" + dim("Superuser — full access to everything (bypasses privilege checks).")

    out = title + "\n\n"

    sch_rows = [[s, _schema_codes(u, cr)] for s, u, cr in schemas if u or cr]
    if sch_rows:
        out += dim("Schemas") + "\n"
        out += render_table(["schema", "access"], sch_rows, header_sgr=hdr) + "\n"
        out += dim(_SCHEMA_LEGEND) + "\n\n"

    accessible = [(name, privs) for name, privs in tables if privs]
    if accessible:
        out += dim("Tables") + "\n"
        out += render_table(
            ["table", "privileges"], [[name, _spell(privs)] for name, privs in accessible],
            header_sgr=hdr,
        ) + "\n"
    no_access = len(tables) - len(accessible)
    if no_access:
        out += dim(f"({no_access} more table(s) with no access)") + "\n"
    if not sch_rows and not accessible:
        out += dim("No object access.") + "\n"
    return out
