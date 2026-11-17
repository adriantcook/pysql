# pysql — common tasks. Run `make` or `make help` to list them.
# Most targets are thin wrappers over uv / ruff / PyInstaller.

.DEFAULT_GOAL := help
.PHONY: help sync run lint format build binary dev install install-binary uninstall clean

# Where `make install-binary` drops the standalone binary (override: make install-binary PREFIX=~/.local)
PREFIX ?= /usr/local

# Copy instead of hardlink from uv's cache — silences the "Failed to hardlink"
# warning when the cache and target are on different filesystems. Exported, so it
# applies to every uv call below (and to scripts/build-binary.sh). Equivalent to
# passing --link-mode=copy on each command.
export UV_LINK_MODE := copy

help: ## List the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

sync: ## Install dependencies into the local .venv
	uv sync

run: ## Run pysql from the source tree (pass args with ARGS="@profile")
	uv run pysql $(ARGS)

lint: ## Lint the codebase with ruff
	uv run ruff check .

format: ## Auto-format the codebase with ruff (reformats in place)
	uv run ruff format .

build: ## Build a wheel + sdist into dist/
	uv build

binary: ## Build a standalone single-file binary (dist/pysql, no Python needed to run)
	./scripts/build-binary.sh

dev: ## Dev install: `pysql` on PATH tracking the source tree live (no rebuilds)
	uv tool install --force --editable .

install: ## Install `pysql` onto your PATH via uv tool (fresh build, no sudo)
	# --no-cache forces a rebuild: the version string stays 0.1.0 between edits,
	# so without it uv would reinstall a cached wheel and ship stale code.
	uv tool install --force --reinstall --no-cache .

install-binary: binary ## Build the binary and install it to $(PREFIX)/bin (uses sudo)
	sudo install -m 0755 dist/pysql $(PREFIX)/bin/pysql

uninstall: ## Remove the uv-tool install of pysql
	uv tool uninstall pysql

clean: ## Remove build artifacts (dist/, build/, *.spec)
	rm -rf dist build *.spec
