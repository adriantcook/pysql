"""RDS IAM authentication — generate a short-lived DB auth token.

For profiles with ``auth = rds-iam`` the password isn't stored; instead a token
(valid ~15 minutes) is generated at connect time from the caller's AWS
credentials. We use boto3's ``generate_db_auth_token`` (an optional dependency,
installed via the ``rds`` extra: ``pip install "pysql[rds]"``).

The token is what Postgres receives as the password; IAM auth also requires TLS,
which the profile layer enforces (``sslmode=require`` by default).
"""

from __future__ import annotations


class RdsAuthError(Exception):
    """Raised when an RDS IAM auth token can't be generated."""


def generate_auth_token(host: str, port, user: str,
                        region: str | None, aws_profile: str | None) -> str:
    """Return an RDS IAM auth token, or raise :class:`RdsAuthError`."""
    if not (host and user):
        raise RdsAuthError("rds-iam profile needs a hostname and username")
    try:
        import boto3
    except ImportError as exc:
        raise RdsAuthError(
            'boto3 is required for rds-iam auth — install it with '
            '`pip install "pysql[rds]"` (or `uv sync --extra rds`)'
        ) from exc
    try:
        aws = boto3.Session(profile_name=aws_profile or None, region_name=region or None)
        client = aws.client("rds")
        return client.generate_db_auth_token(DBHostname=host, Port=int(port or 5432), DBUsername=user)
    except Exception as exc:  # ProfileNotFound, NoCredentials, NoRegion, …
        raise RdsAuthError(f"could not generate RDS auth token: {exc}") from exc
