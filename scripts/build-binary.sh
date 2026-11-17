#!/usr/bin/env bash
# Build a single-file, standalone `pysql` binary with PyInstaller.
#
# The result (dist/pysql) bundles Python, libpq (via psycopg[binary]) and all
# dependencies, so it runs on a machine with no Python installed. Install it with:
#
#     sudo install -m 0755 dist/pysql /usr/local/bin/pysql
#
# Note: the binary is built for *this* OS/arch/libc — build on a machine that
# matches your targets (e.g. a glibc Linux box for glibc Linux users). RDS IAM
# auth (boto3) is intentionally excluded to keep the binary lean; use the pip/uv
# install for that.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Syncing the build toolchain (pyinstaller)…"
uv sync --group build --quiet

echo "Building dist/pysql …"
uv run --group build pyinstaller \
    --onefile \
    --name pysql \
    --clean --noconfirm \
    --collect-all psycopg \
    --collect-all psycopg_binary \
    --collect-all prompt_toolkit \
    --exclude-module boto3 \
    --exclude-module botocore \
    --exclude-module tkinter \
    --specpath build \
    packaging/pysql_entry.py

echo
echo "Built: dist/pysql"
echo "Install with:  sudo install -m 0755 dist/pysql /usr/local/bin/pysql"
