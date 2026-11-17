"""Human-friendly role / membership display for ``\\du``.

psql's own ``\\du`` prints an "Attributes" blob and a flat ``Member of {…}`` set
that tells you nothing about the shape of your roles. We reuse the name but
instead:

* separate **login users** from **group roles** (Postgres makes no formal
  distinction, but ``rolcanlogin`` is the one people mean by "user" vs "group"),
* spell the privilege attributes out as words, not catalog column names, and
* draw the membership **tree**, so "who is in what" is a picture, not a puzzle.

Pure rendering: it takes already-fetched role rows and membership edges and
returns a text block, so it is trivial to test without a database.
"""

from __future__ import annotations

from typing import Any

from .table import render_table


def _badges(role: dict) -> list[str]:
    """Privilege attributes, spelled out, most-powerful first."""
    out = []
    if role["superuser"]:
        out.append("Superuser")
    if role["createrole"]:
        out.append("Create role")
    if role["createdb"]:
        out.append("Create DB")
    if role["replication"]:
        out.append("Replication")
    if role["bypassrls"]:
        out.append("Bypass RLS")
    if not role["inherit"]:
        out.append("No inherit")
    return out


def _valid_until(value: Any) -> str:
    if value is None:
        return "never"
    text = str(value)
    return "never" if "infinity" in text else text


def _mk_color(color: bool):
    def c(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    return c


def _member_of(name: str, members: list[tuple[str, str, bool]]) -> list[tuple[str, bool]]:
    """Groups ``name`` belongs to, as (group, admin_option)."""
    return [(grp, admin) for grp, m, admin in members if m == name]


def render_roles(roles: list[dict], members: list[tuple[str, str, bool]], color: bool) -> str:
    """Overview: a roles table plus the membership tree."""
    c = _mk_color(color)
    dim = lambda t: c(t, "2")  # noqa: E731
    hdr = "1" if color else None  # bold column headers

    # Table: hide the built-in pg_* roles (noise); they still show up in the
    # tree / "member of" if a real role is granted one.
    visible = [r for r in roles if not r["name"].startswith("pg_")]
    table_rows = []
    for r in roles_sorted(visible):
        kind = "user" if r["can_login"] else "group"
        attrs = ", ".join(_badges(r)) or "—"
        belongs = ", ".join(grp for grp, _ in _member_of(r["name"], members)) or "—"
        table_rows.append([r["name"], kind, attrs, belongs])

    block = c("Roles", "1;32") + "\n\n"
    block += render_table(["role", "type", "attributes", "member of"], table_rows,
                          header_sgr=hdr) + "\n"

    tree = _membership_tree(roles, members, c)
    if tree:
        block += "\n" + dim("Membership") + "\n" + tree
    return block


def roles_sorted(roles: list[dict]) -> list[dict]:
    return sorted(roles, key=lambda r: r["name"])


def _membership_tree(roles, members, c) -> str:
    """Forest of group → members. A node that owns members is a 'group'."""
    dim = lambda t: c(t, "2")  # noqa: E731
    children: dict[str, list[tuple[str, bool]]] = {}
    for grp, m, admin in members:
        children.setdefault(grp, []).append((m, admin))
    if not children:
        return ""

    is_member = {m for _, m, _ in members}
    # Roots: groups that have members but aren't themselves inside another group.
    roots = sorted(g for g in children if g not in is_member)
    has_children = set(children)

    lines: list[str] = []

    def walk(name: str, pad: str, connector: str, admin: bool, seen: frozenset) -> None:
        label = c(name, "36") if name in has_children else name
        if admin:
            label += dim(" *")
        lines.append(pad + connector + label)
        if name in seen:  # cycle guard (shouldn't happen; Postgres forbids them)
            return
        kids = sorted(children.get(name, []))
        child_pad = pad + ("" if not connector else ("   " if _is_last(connector) else "│  "))
        for i, (child, child_admin) in enumerate(kids):
            last = i == len(kids) - 1
            walk(child, child_pad, "└─ " if last else "├─ ", child_admin, seen | {name})

    for root in roots:
        walk(root, "", "", False, frozenset())

    out = "\n".join(lines)
    if any(admin for _, _, admin in members):
        out += "\n" + dim("* = WITH ADMIN OPTION")
    return out


def _is_last(connector: str) -> bool:
    return connector.startswith("└")


def render_role_detail(
    name: str, roles: list[dict], members: list[tuple[str, str, bool]], color: bool
) -> str | None:
    """Detail for one role, or ``None`` if there is no such role."""
    c = _mk_color(color)
    role = next((r for r in roles if r["name"] == name), None)
    if role is None:  # fall back to a case-insensitive match
        role = next((r for r in roles if r["name"].lower() == name.lower()), None)
    if role is None:
        return None

    kind = "login user" if role["can_login"] else "group role"
    belongs = _member_of(role["name"], members)
    owns = sorted(m for grp, m, _ in members if grp == role["name"])

    rows = [["type", kind]]
    rows.append(["attributes", ", ".join(_badges(role)) or "none"])
    rows.append(
        ["member of", ", ".join(g + (" (admin)" if a else "") for g, a in belongs) or "—"]
    )
    if owns:
        rows.append(["members", ", ".join(owns)])
    rows.append(["inherits privileges", "yes" if role["inherit"] else "no"])
    rows.append(
        ["connection limit", "unlimited" if role["connlimit"] < 0 else str(role["connlimit"])]
    )
    rows.append(["valid until", _valid_until(role["validuntil"])])

    title = c(f'Role "{role["name"]}"  —  {kind}', "1;32")
    hdr = "1" if color else None
    return title + "\n\n" + render_table(["field", "value"], rows, header_sgr=hdr)
