"""Reversible encryption for profile passwords.

A machine-local key (``~/.pysql_key``, created on first use, ``chmod 600``)
encrypts and decrypts the password strings stored in the profile file. This
keeps plaintext passwords out of the profile file and protects it if it's copied
elsewhere — but it is *at-rest obfuscation*, not a vault: anyone who can read
both the profile file and the key file on this machine can recover the password.
For stronger secrecy, an OS keychain or a typed master passphrase would be
needed; this trades that for being able to decrypt automatically at connect time.

Uses Fernet (AES-128-CBC + HMAC-SHA256, authenticated) from ``cryptography``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_PATH = Path.home() / ".pysql_key"


class CryptoError(Exception):
    """Raised when a password can't be decrypted (wrong key or corrupt token)."""


def _load_or_create_key(path: Path | None = None) -> bytes:
    """Return the Fernet key, generating and saving it (0600) if absent."""
    path = path or KEY_PATH
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    # Create with owner-only permissions from the start where the OS honours it.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except FileExistsError:  # created concurrently; use what's there
        return path.read_bytes().strip()
    _restrict(path)
    return key


def _restrict(path: Path) -> None:
    """Best-effort tighten to owner read/write only (no-op where unsupported)."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass


def encrypt(plaintext: str, *, key_path: Path | None = None) -> str:
    """Encrypt ``plaintext`` into a Fernet token (safe to store in the file)."""
    return Fernet(_load_or_create_key(key_path)).encrypt(plaintext.encode()).decode()


def decrypt(token: str, *, key_path: Path | None = None) -> str:
    """Decrypt a Fernet token back to the password, or raise ``CryptoError``."""
    try:
        return Fernet(_load_or_create_key(key_path)).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise CryptoError(
            "could not decrypt password — the key in ~/.pysql_key may have "
            "changed, or the value isn't a \\enc token"
        ) from exc
