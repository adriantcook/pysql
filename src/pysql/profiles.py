"""Named connection profiles for ``\\C`` / ``\\add`` / ``pysql @name``.

Preferred format is **YAML** at ``~/.pysql_profiles.yaml`` (or ``.yml``), one
mapping per profile::

    engineering-test-core:
      username: myuser
      password: gAAAAAB...        # a \\enc token (see crypto.py)
      host: my-database.host.name.com    # the writer endpoint
      reader: my-reader.host.name.com    # optional; when set, connects here by default
      default_mode: writer         # optional; override the reader default (reader|writer)
      port: 5432
      database: default

When a profile has a ``reader:`` endpoint, pysql connects to it by default (a
safe, read-only-by-convention landing spot) and ``\\w`` toggles to the writer and
back. Set ``default_mode: writer`` to land on the writer instead. The prompt
shows a red ``*`` while on the writer.

For backward compatibility the older **INI** file ``~/.pysql_profiles`` is still
read when no YAML file is present, so existing setups keep working untouched::

    [engineering-test-core]
    username = myuser
    password = gAAAAAB...        ; a \\enc token
    hostname = my-database.host.name.com
    port     = 5432
    database = default

The password is an encrypted token produced by ``\\enc``; it's decrypted to a
libpq conninfo string only at connect time. A missing/blank password is allowed
(for trust/peer/.pgpass auth). A profile may also carry a nested ``tunnel:``
block (SSM port-forwarding) — parsed here, acted on by the tunnel layer.
"""

from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from psycopg.conninfo import make_conninfo

from .crypto import decrypt
from .rds import generate_auth_token

# Legacy INI path, and the YAML paths that take precedence over it.
PROFILE_PATH = Path.home() / ".pysql_profiles"
YAML_PATHS = (Path.home() / ".pysql_profiles.yaml", Path.home() / ".pysql_profiles.yml")


@dataclass(frozen=True)
class Profile:
    name: str
    username: str
    password: str  # encrypted token, or "" for none
    hostname: str  # the writer endpoint (the `host:` field)
    port: str
    database: str
    auth: str = ""          # "rds-iam" for IAM token auth, else static password
    region: str = ""        # AWS region (rds-iam)
    aws_profile: str = ""   # AWS credentials profile (rds-iam)
    sslmode: str = ""       # libpq sslmode; defaults to "require" for rds-iam
    reader: str = ""        # optional reader endpoint; when set, connects here by default
    default_mode: str = ""  # "reader"/"writer"; which endpoint to use by default (reader)
    tunnel: dict | None = field(default=None)  # nested SSM tunnel block (see tunnel layer)


def _s(value: object) -> str:
    """YAML scalars may be int/bool/None; coerce to a clean string ('' for None)."""
    return "" if value is None else str(value)


def active_profile_path() -> Path:
    """The profile file currently in effect: the YAML one if present, else INI.

    When none exists yet, returns the preferred (YAML) path — that's what a fresh
    ``\\add`` creates.
    """
    for p in YAML_PATHS:
        if p.exists():
            return p
    if PROFILE_PATH.exists():
        return PROFILE_PATH
    return YAML_PATHS[0]


def _is_yaml(path: Path) -> bool:
    return path.suffix.lower() in (".yaml", ".yml")


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    """Parse the profile file into ``{name: Profile}`` (empty if none exists).

    With no ``path`` the YAML file is preferred, falling back to the legacy INI.
    An explicit ``path`` is parsed by its extension (``.yaml``/``.yml`` → YAML).
    """
    if path is None:
        path = active_profile_path()
    if not path.exists():
        return {}
    return _load_yaml(path) if _is_yaml(path) else _load_ini(path)


def _profile_from_mapping(name: str, body: dict) -> Profile:
    """Build a Profile from a YAML mapping (host/hostname both accepted)."""
    tunnel = body.get("tunnel")
    default_mode = _s(body.get("default_mode")).strip().lower()
    if default_mode not in ("", "reader", "writer"):
        default_mode = ""  # unknown value → fall back to the safe default (reader)
    return Profile(
        name=name,
        username=_s(body.get("username")),
        password=_s(body.get("password")),
        hostname=_s(body.get("host") if body.get("host") is not None else body.get("hostname")),
        port=_s(body.get("port")),
        database=_s(body.get("database")),
        auth=_s(body.get("auth")).strip().lower(),
        region=_s(body.get("region")),
        aws_profile=_s(body.get("aws_profile")),
        sslmode=_s(body.get("sslmode")).strip().lower(),
        reader=_s(body.get("reader")).strip(),
        default_mode=default_mode,
        tunnel=tunnel if isinstance(tunnel, dict) else None,
    )


_WARNED: set[Path] = set()  # paths we've already complained about (don't spam completers)


def _warn_once(path: Path, message: str) -> None:
    """Print a one-time stderr warning for a broken profile file."""
    if path in _WARNED:
        return
    _WARNED.add(path)
    print(f"pysql: {message}", file=sys.stderr)


def _load_yaml(path: Path) -> dict[str, Profile]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        _warn_once(path, f"could not parse {path} as YAML — no profiles loaded from it. "
                         f"It may contain INI-style [sections]; YAML uses 'name:' then "
                         f"indented 'key: value'. ({detail})")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        _warn_once(path, f"ignoring {path}: expected a mapping of profiles at the top level, "
                         f"got {type(data).__name__}")
        return {}
    profiles: dict[str, Profile] = {}
    for name, body in data.items():
        if isinstance(body, dict):
            profiles[str(name)] = _profile_from_mapping(str(name), body)
    return profiles


def _load_ini(path: Path) -> dict[str, Profile]:
    # interpolation=None: values are literal (a stray '%' must not be special).
    # inline_comment_prefixes=(';',): honour "key = value  ; comment" (but only
    # ';', so it never clips a value that legitimately contains '#').
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    parser.read(path)
    profiles: dict[str, Profile] = {}
    for name in parser.sections():
        profiles[name] = _profile_from_mapping(name, dict(parser[name]))
    return profiles


def save_profile(profile: Profile, path: Path | None = None) -> None:
    """Append ``profile`` to the active profile file, preserving existing text.

    Appends rather than rewrites so hand-written comments and ordering survive.
    Writes YAML or INI to match the target file's format (a fresh file is YAML).
    Creates the file and tightens it to owner-only.
    """
    if path is None:
        path = active_profile_path()
    is_new = not path.exists()
    block = _yaml_block(profile) if _is_yaml(path) else _ini_block(profile)
    with path.open("a", encoding="utf-8") as f:
        if not is_new:
            f.write("\n")
        f.write(block)
    # The file now holds an (encrypted) secret — keep it owner-only.
    try:
        path.chmod(0o600)
    except OSError:
        pass


def set_profile_password(name: str, encrypted: str, path: Path | None = None) -> None:
    """Set (add or replace) the encrypted ``password`` of an *existing* profile.

    Edits the active profile file in place, leaving other profiles, comments and
    ordering untouched (unlike ``save_profile``, which appends a whole new one).
    Raises ``KeyError`` if ``name`` isn't in the file, ``ValueError`` if its block
    can't be edited safely, or ``OSError`` on a write failure.
    """
    if path is None:
        path = active_profile_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    editor = _yaml_set_password if _is_yaml(path) else _ini_set_password
    new_lines = editor(lines, name, encrypted)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # it now holds a secret — keep it owner-only
    except OSError:
        pass


def _yaml_set_password(lines: list[str], name: str, token: str) -> list[str]:
    """Add/replace ``password:`` under the block-style ``name:`` key."""
    start = None
    for i, ln in enumerate(lines):
        if ln[:1] and not ln[:1].isspace() and not ln.lstrip().startswith("#"):
            key, sep, rest = ln.partition(":")
            if sep and key.strip() == name:
                # A trailing "# comment" after "name:" is fine; a real value isn't
                # (that would be an inline/flow mapping we can't edit line-wise).
                if rest.strip() and not rest.lstrip().startswith("#"):
                    raise ValueError(f"profile {name!r} isn't a block mapping")
                start = i
                break
    if start is None:
        raise KeyError(name)
    # The block runs until the next column-0 key (or end of file).
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln[:1] and not ln[:1].isspace() and not ln.lstrip().startswith("#"):
            end = j
            break
    # Match the block's own indentation (first non-blank, non-comment child).
    indent = "  "
    for k in range(start + 1, end):
        if lines[k].strip() and not lines[k].lstrip().startswith("#"):
            indent = lines[k][: len(lines[k]) - len(lines[k].lstrip())]
            break
    new_line = f"{indent}password: {token}"
    for k in range(start + 1, end):
        body = lines[k].lstrip()
        if not body.startswith("#") and body.partition(":")[0].strip() == "password":
            lines[k] = new_line
            return lines
    lines.insert(start + 1, new_line)
    return lines


def _ini_set_password(lines: list[str], name: str, token: str) -> list[str]:
    """Add/replace ``password = …`` inside the ``[name]`` INI section."""
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == f"[{name}]":
            start = i
            break
    if start is None:
        raise KeyError(name)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("["):
            end = j
            break
    new_line = f"password = {token}"
    for k in range(start + 1, end):
        body = lines[k].strip()
        if not body or body.startswith((";", "#")) or "=" not in body:
            continue
        if body.split("=", 1)[0].strip() == "password":
            lines[k] = new_line
            return lines
    lines.insert(start + 1, new_line)
    return lines


def _profile_body(profile: Profile) -> dict:
    """The ordered field mapping written for a profile (format-independent)."""
    body: dict[str, object] = {}
    if profile.auth:  # IAM profile: no stored password, write AWS coordinates
        body["auth"] = profile.auth
        body["username"] = profile.username
        body["host"] = profile.hostname
        if profile.reader:
            body["reader"] = profile.reader
        body["port"] = int(profile.port) if profile.port.isdigit() else profile.port
        body["database"] = profile.database
        for key in ("region", "aws_profile", "sslmode"):
            if getattr(profile, key):
                body[key] = getattr(profile, key)
    else:
        body["username"] = profile.username
        body["password"] = profile.password
        body["host"] = profile.hostname
        if profile.reader:
            body["reader"] = profile.reader
        body["port"] = int(profile.port) if profile.port.isdigit() else profile.port
        body["database"] = profile.database
        if profile.sslmode:
            body["sslmode"] = profile.sslmode
    if profile.default_mode:
        body["default_mode"] = profile.default_mode
    return body


def _yaml_block(profile: Profile) -> str:
    # safe_dump handles any needed quoting; sort_keys=False keeps our order.
    return yaml.safe_dump({profile.name: _profile_body(profile)},
                          sort_keys=False, default_flow_style=False, allow_unicode=True)


def _ini_block(profile: Profile) -> str:
    # INI uses "hostname"; everything else maps one-to-one from the body.
    lines = [f"[{profile.name}]"]
    for key, value in _profile_body(profile).items():
        lines.append(f"{'hostname' if key == 'host' else key} = {value}")
    return "".join(line + "\n" for line in lines)


def profile_conninfo(
    profile: Profile,
    *,
    endpoint_host: str | None = None,
    connect_host: str | None = None,
    connect_port: int | str | None = None,
    remote_port: int | str | None = None,
) -> str:
    """Build a libpq conninfo string for a profile.

    For ``auth = rds-iam`` a fresh IAM token is generated as the password and TLS
    is enforced; otherwise the stored password is decrypted if present. Raises
    ``crypto.CryptoError`` (bad stored password) or ``rds.RdsAuthError`` (token
    generation failed). Called on every connect, so IAM tokens never go stale.

    ``endpoint_host`` is the *real* server to reach — ``profile.hostname`` (the
    writer) by default, or ``profile.reader`` when the session is in reader mode.
    It drives both the TCP target and the host the IAM token is signed for.

    When the profile is reached through a tunnel, ``connect_host`` / ``connect_port``
    point the actual TCP connection at the local tunnel endpoint, while the IAM
    token is still signed for the *real* endpoint (``endpoint_host`` and
    ``remote_port``) — that's what RDS validates.
    """
    endpoint = endpoint_host or profile.hostname
    kwargs = {
        "host": connect_host or endpoint or None,
        "port": connect_port or profile.port or None,
        "dbname": profile.database or None,
        "user": profile.username or None,
    }

    if profile.auth == "rds-iam":
        kwargs["password"] = generate_auth_token(
            endpoint, remote_port or profile.port, profile.username,
            profile.region, profile.aws_profile,
        )
        kwargs["sslmode"] = profile.sslmode or "require"  # IAM auth requires TLS
    else:
        if profile.password:
            kwargs["password"] = decrypt(profile.password)
        if profile.sslmode:
            kwargs["sslmode"] = profile.sslmode

    # Drop unset keys so libpq can fall back to its own defaults.
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    return make_conninfo("", **kwargs)
