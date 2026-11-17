"""Auto-managed AWS SSM port-forwarding tunnels for profiles with a ``tunnel:``
block.

A profile can reach a private RDS/Aurora endpoint through a bastion EC2 instance
without SSH keys or open ports, using **SSM Session Manager** port forwarding::

    default:
      username: myuser
      database: mydb
      tunnel:
        via: ssm                       # the only backend today (pluggable)
        aws_profile: engineering       # falls back to the profile's aws_profile
        region: ap-southeast-2         # falls back to the profile's region
        bastion: i-0abc123             # SSM target: instance-id, tag:Key=Value,
                                       #   or several tags: tag:Key1=V1,Key2=V2 (all must match)
        host: engineering-host.rds.amazonaws.com   # the private endpoint (defaults to the profile host)
        remote_port: 5432              # defaults to the profile port, else 5432
        local_port: 0                  # 0 / omitted = pick a free local port
        keepalive: true                # heartbeat the session (default on; false disables)
        keepalive_interval: 10         # seconds between heartbeats (default 10)

When the profile is opened, :class:`Tunnel` shells out to ``aws ssm start-session
--document-name AWS-StartPortForwardingSessionToRemoteHost``, waits for the local
port to accept connections, and pysql then connects to ``127.0.0.1:<local_port>``.
Closing the session (``\\close`` / quit) stops the tunnel. Needs the AWS CLI and
its ``session-manager-plugin`` installed locally.

While the tunnel is up a daemon **keep-alive** thread briefly connects to the
local port every ``keepalive_interval`` seconds, so an otherwise idle SSM session
isn't torn down for inactivity (the same trick the standalone ``db`` helper uses).
It is on by default at 10s. The built-in defaults are overridden globally by
``keepalive`` / ``keepalive_interval`` in ``~/.pysqlrc`` ``[settings]`` (applied via
:func:`set_keepalive_defaults`), and a profile's own ``tunnel:`` block overrides
both.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace

# Built-in keep-alive defaults. ``set_keepalive_defaults`` folds the global
# ~/.pysqlrc [settings] over these at startup; a profile's tunnel: block then
# overrides the result per tunnel (see ``tunnel_from_profile``).
_DEFAULT_KEEPALIVE = True
_DEFAULT_KEEPALIVE_INTERVAL = 10.0

_global_keepalive = _DEFAULT_KEEPALIVE
_global_keepalive_interval = _DEFAULT_KEEPALIVE_INTERVAL


def set_keepalive_defaults(
    enabled: bool | None = None, interval: float | None = None
) -> None:
    """Override the built-in keep-alive defaults from global ``~/.pysqlrc`` settings.

    Called once at startup with the values :func:`config.load_config` parsed from
    ``[settings]`` (either may be ``None`` / absent, leaving that built-in default
    in place). A profile's ``tunnel:`` block still overrides whatever is set here.
    A non-positive ``interval`` is ignored.
    """
    global _global_keepalive, _global_keepalive_interval
    if enabled is not None:
        _global_keepalive = bool(enabled)
    if interval is not None and interval > 0:
        _global_keepalive_interval = float(interval)


class TunnelError(Exception):
    """Raised when a tunnel can't be configured or established."""


@dataclass(frozen=True)
class TunnelConfig:
    via: str  # "ssm"
    bastion: str  # SSM target: an instance-id, or "tag:Key=Value"
    host: str  # the remote (private) database endpoint
    remote_port: int  # the remote database port
    local_port: int  # desired local port, or 0 to auto-pick
    region: str  # AWS region (may be "")
    aws_profile: str  # AWS credentials profile (may be "")
    keepalive: bool = True  # heartbeat the session so idle doesn't reap it
    keepalive_interval: float = 10.0  # seconds between heartbeats (>0)


def tunnel_from_profile(profile) -> Tunnel | None:
    """Build a :class:`Tunnel` from a profile's ``tunnel:`` block, or ``None``.

    Raises :class:`TunnelError` for a malformed or unsupported block. Region /
    aws_profile / host / remote_port fall back to the profile's own fields so an
    IAM-auth + tunnel profile needn't repeat them.
    """
    block = getattr(profile, "tunnel", None)
    if not block:
        return None
    via = str(block.get("via", "ssm")).strip().lower()
    if via != "ssm":
        raise TunnelError(f'unsupported tunnel type "{via}" (only "ssm" is supported)')
    bastion = str(block.get("bastion", "")).strip()
    if not bastion:
        raise TunnelError(
            'tunnel needs a "bastion" (an SSM instance-id, tag:Key=Value, '
            "or several tags: tag:Key1=V1,Key2=V2)"
        )
    host = str(block.get("host") or profile.hostname or "").strip()
    if not host:
        raise TunnelError('tunnel needs a "host" (the remote database endpoint)')
    try:
        remote_port = int(block.get("remote_port") or profile.port or 5432)
        local_port = int(block.get("local_port") or 0)
    except (TypeError, ValueError) as exc:
        raise TunnelError(f"tunnel port must be a number: {exc}") from exc
    region = str(block.get("region") or profile.region or "").strip()
    aws_profile = str(block.get("aws_profile") or profile.aws_profile or "").strip()
    keepalive, keepalive_interval = _keepalive_from_block(block)
    return Tunnel(
        TunnelConfig(
            via, bastion, host, remote_port, local_port, region, aws_profile,
            keepalive, keepalive_interval,
        )
    )


def _coerce_bool(value, default: bool) -> bool:
    """A YAML bool passes through; a string is read like a config on/off value;
    anything unrecognised falls back to ``default`` (a typo never breaks a tunnel)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"on", "true", "yes", "1"}:
        return True
    if text in {"off", "false", "no", "0"}:
        return False
    return default


def _keepalive_from_block(block: dict) -> tuple[bool, float]:
    """Resolve ``(keepalive, keepalive_interval)`` for a tunnel block.

    A key present in the block wins; otherwise the global default (built-in,
    already overlaid with ``~/.pysqlrc`` by :func:`set_keepalive_defaults`) applies.
    A non-numeric or non-positive ``keepalive_interval`` in the block is an error,
    mirroring how a bad port is rejected above.
    """
    if "keepalive" in block:
        keepalive = _coerce_bool(block.get("keepalive"), _global_keepalive)
    else:
        keepalive = _global_keepalive
    if "keepalive_interval" in block:
        try:
            interval = float(block.get("keepalive_interval"))
        except (TypeError, ValueError) as exc:
            raise TunnelError(
                f"tunnel keepalive_interval must be a number: {exc}"
            ) from exc
        if interval <= 0:
            raise TunnelError("tunnel keepalive_interval must be greater than 0")
    else:
        interval = _global_keepalive_interval
    return keepalive, interval


def _free_port() -> int:
    """Ask the OS for an unused local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    """One SSM port-forwarding session, owned by a DB session."""

    def __init__(self, cfg: TunnelConfig) -> None:
        self.cfg = cfg
        self.local_port = cfg.local_port
        self._proc: subprocess.Popen | None = None
        self._errfile = None
        self._ka_thread: threading.Thread | None = None
        self._ka_stop = threading.Event()

    # --- lifecycle --------------------------------------------------------

    def start(self, ready_timeout: float = 25.0) -> None:
        """Launch the tunnel and block until the local port is accepting."""
        if not shutil.which("aws"):
            raise TunnelError(
                "the AWS CLI (`aws`) is not installed — it's needed for SSM tunnels"
            )
        if not shutil.which("session-manager-plugin"):
            raise TunnelError(
                "`session-manager-plugin` is not installed — AWS SSM port forwarding "
                "needs it (see the AWS 'Install the Session Manager plugin' docs)"
            )
        target = self._resolve_target()
        # ``local_port`` is mutable (see rebind): honour the current value, which
        # starts as cfg.local_port and is updated by a successful start/rebind.
        port = self.local_port or _free_port()
        params = (
            f"host={self.cfg.host},portNumber={self.cfg.remote_port},"
            f"localPortNumber={port}"
        )
        cmd = [
            "aws",
            "ssm",
            "start-session",
            "--target",
            target,
            "--document-name",
            "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters",
            params,
        ]
        if self.cfg.region:
            cmd += ["--region", self.cfg.region]
        if self.cfg.aws_profile:
            cmd += ["--profile", self.cfg.aws_profile]

        # a file (not a pipe) can't fill up and block; it outlives this call
        # (closed in stop()), so a context manager would be wrong here.
        self._errfile = tempfile.TemporaryFile()  # noqa: SIM115
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=self._errfile,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.stop()  # closes the errfile we just opened
            raise TunnelError(
                f"could not launch `aws ssm start-session`: {exc}"
            ) from exc
        self.local_port = port
        self._wait_ready(ready_timeout)
        self._start_keepalive()  # only after the port is confirmed accepting

    def stop(self) -> None:
        """Terminate the tunnel (idempotent)."""
        self._stop_keepalive()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._errfile is not None:
            try:
                self._errfile.close()
            except OSError:
                pass
            self._errfile = None

    def point_at(self, host: str) -> None:
        """Aim a *not-yet-started* tunnel at a remote ``host``.

        Used to select the reader vs. writer endpoint before the initial
        ``start()``. To change the remote host of a *running* tunnel, use
        :meth:`repoint`, which restarts it.
        """
        self.cfg = replace(self.cfg, host=host)

    def repoint(self, host: str, ready_timeout: float = 25.0) -> None:
        """Restart the tunnel forwarding to a new remote ``host``, same local port.

        Unlike :meth:`rebind`, the local port is reused, so the old listener must
        be stopped before the new one can bind it (a brief gap — the caller is
        reconnecting the database anyway). If the new host can't be reached the
        tunnel is restored to its previous host; a :class:`TunnelError` is raised
        either way on failure.
        """
        if host == self.cfg.host:
            return
        old_host = self.cfg.host
        self.stop()  # free the local port (kept in self.local_port) for reuse
        self.cfg = replace(self.cfg, host=host)
        try:
            self.start(ready_timeout)  # rebinds self.local_port, forwards to `host`
        except BaseException:
            # Put the forward back on the old host so the session can recover.
            self.cfg = replace(self.cfg, host=old_host)
            try:
                self.start(ready_timeout)
            except BaseException:
                pass  # couldn't even restore; the caller will surface the failure
            raise

    def rebind(self, local_port: int, ready_timeout: float = 25.0) -> int:
        """Move the tunnel to ``local_port`` (0 = auto-pick a free port).

        The new local listener is brought up and verified *before* the old one
        is torn down, so if the requested port can't be bound the existing
        tunnel keeps running and a :class:`TunnelError` is raised. Returns the
        new local port on success.
        """
        if not self.active:  # nothing to preserve — just (re)start on the port
            self.local_port = local_port
            self.start(ready_timeout)
            return self.local_port

        old_proc, old_errfile, old_port = self._proc, self._errfile, self.local_port
        self._proc = None  # detach the live handles so start() can't clobber them
        self._errfile = None
        self.local_port = local_port
        try:
            self.start(ready_timeout)
        except BaseException:
            self.stop()  # clear any half-started new listener
            self._proc, self._errfile, self.local_port = old_proc, old_errfile, old_port
            self._start_keepalive()  # the restored tunnel keeps its heartbeat
            raise
        # The new listener is accepting — retire the old process and its errfile.
        if old_proc is not None and old_proc.poll() is None:
            old_proc.terminate()
            try:
                old_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                old_proc.kill()
        if old_errfile is not None:
            try:
                old_errfile.close()
            except OSError:
                pass
        return self.local_port

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def endpoint(self) -> tuple[str, int]:
        """The local ``(host, port)`` to connect to."""
        return ("127.0.0.1", self.local_port)

    def describe(self) -> str:
        return f"ssm {self.cfg.bastion} → {self.cfg.host}:{self.cfg.remote_port}"

    # --- internals --------------------------------------------------------

    def _resolve_target(self) -> str:
        """An instance-id passes through; a ``tag:`` spec is looked up via EC2.

        One tag (``tag:Key=Value``) or several, comma-separated
        (``tag:Key1=Val1,Key2=Val2``); multiple tags must *all* match (AND),
        which is how AWS combines repeated ``--filters``. Whitespace around each
        ``Key=Value`` is ignored, so ``tag:env=prod, role=bastion`` is fine.
        """
        bastion = self.cfg.bastion
        if not bastion.startswith("tag:"):
            return bastion
        spec = bastion[len("tag:") :]
        tag_filters = []
        for pair in spec.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                raise TunnelError(
                    'bastion tag must look like "tag:Key=Value" '
                    '(multiple: "tag:Key1=Val1,Key2=Val2")'
                )
            key, value = pair.split("=", 1)
            tag_filters.append(f"Name=tag:{key.strip()},Values={value}")
        cmd = [
            "aws",
            "ec2",
            "describe-instances",
            "--filters",
            *tag_filters,
            "Name=instance-state-name,Values=running",
            "--query",
            "Reservations[].Instances[].InstanceId",
            "--output",
            "text",
        ]
        if self.cfg.region:
            cmd += ["--region", self.cfg.region]
        if self.cfg.aws_profile:
            cmd += ["--profile", self.cfg.aws_profile]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TunnelError(f"could not resolve bastion {bastion}: {exc}") from exc
        if res.returncode != 0:
            raise TunnelError(
                f"could not resolve bastion {bastion}: "
                + (res.stderr.strip() or res.stdout.strip() or "aws ec2 failed")
            )
        ids = res.stdout.split()
        if not ids:
            raise TunnelError(f"no running instance matches {bastion}")
        return ids[0]

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self._proc.poll()  # type: ignore[union-attr]
            if rc is not None:
                err = self._read_err()
                self.stop()  # close the errfile / reap the process
                raise TunnelError(f"SSM session exited (code {rc}). {err}".rstrip())
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.local_port), timeout=1
                ):
                    return
            except OSError:
                time.sleep(0.3)
        self.stop()
        raise TunnelError(
            f"tunnel did not become ready within {timeout:.0f}s "
            "(check the bastion id, AWS profile/region, and SSM permissions)"
        )

    def _read_err(self) -> str:
        if self._errfile is None:
            return ""
        try:
            self._errfile.seek(0)
            text = self._errfile.read().decode("utf-8", "replace")
        except OSError:
            return ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    # --- keep-alive -------------------------------------------------------

    def _start_keepalive(self) -> None:
        """Spin up the heartbeat thread (no-op if disabled or already running).

        Idempotent: it first tears down any existing thread, so the ``start()``
        re-entry inside ``rebind``/``repoint`` never leaves two heartbeats racing.
        """
        if not self.cfg.keepalive or self.cfg.keepalive_interval <= 0:
            return
        self._stop_keepalive()
        self._ka_stop.clear()
        self._ka_thread = threading.Thread(
            target=self._keepalive_worker, name="tunnel-keepalive", daemon=True
        )
        self._ka_thread.start()

    def _stop_keepalive(self) -> None:
        """Signal the heartbeat thread to exit and wait briefly for it."""
        self._ka_stop.set()
        thread, self._ka_thread = self._ka_thread, None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2)

    def _keepalive_worker(self) -> None:
        """Briefly connect to the local port every ``keepalive_interval`` seconds.

        That trickle of traffic through the forward keeps an otherwise idle SSM
        session from being reaped (the same trick the standalone ``db`` helper
        uses). Connection errors are swallowed — a genuinely dead tunnel surfaces
        on the next real query — so the daemon thread never crashes the REPL.
        """
        interval = self.cfg.keepalive_interval
        # wait() first: the port was just verified ready, so no immediate poke.
        while not self._ka_stop.wait(interval):
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.local_port), timeout=2
                ):
                    pass
            except OSError:
                pass
