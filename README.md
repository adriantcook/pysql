# pysql

A friendlier `psql` — an interactive PostgreSQL client that tells you a bit more.

Early days. Right now it gives you a `pysql> ` prompt that connects to a
database, runs statements, and prints results with clearer NULLs, query timing,
and the full error diagnostics Postgres sends back.

## Demos

**Connecting, saving a profile, and browsing databases and tables**

![Connecting, saving a profile, listing databases and describing a table](demos/connect.gif)

**Switching between connections and running a query on every open session**

![Switching between profiles and using \run-on-active](demos/connections.gif)

**Saving a profile and hopping between several open connections**

![Saving a profile with \add, listing profiles with \C, and switching connections with \C name and \C -](demos/multi-connect.gif)

**Finding a slow query with `EXPLAIN ANALYZE` and fixing it with an index**

![EXPLAIN ANALYZE showing a sequential scan, then an index scan after adding an index](demos/slow_cast.gif)

## Install & run

```bash
uv sync
uv run pysql "postgresql://user@localhost/mydb"
```

With no argument it falls back to `$DATABASE_URL`, then to standard libpq
environment variables (`PGHOST`, `PGDATABASE`, `PGUSER`, …) and `~/.pgpass`:

```bash
uv run pysql
```

Command-line flags:

```bash
pysql @mydb-profile      # connect using a saved profile (see Connection profiles)
pysql --enc [PASSWORD]    # print an encrypted token for a profile password, then exit
pysql --help              # usage summary
```

`--enc` is the one-shot equivalent of the in-prompt `\enc` — handy for scripting
or when you're not already in a session. Pass the password inline (`pysql --enc
'my pw'`) or omit it to be asked at a hidden prompt (which also keeps it out of
your shell history and the process list). The token is written to stdout so you
can capture it; the hint goes to stderr.

### Installing it as a command

A `Makefile` wraps the common tasks — run `make help` to list them. The quickest
route to a runnable `pysql` is `make install` (PATH install, no sudo); `make
install-binary` builds and installs the standalone binary to `/usr/local/bin`.

The three methods, from simplest to most portable, and what the `make` targets run:

**1. On your PATH with uv** (recommended for most people) — installs into an
isolated environment and drops a launcher in `~/.local/bin`:

```bash
uv tool install .            # from a clone;  or:  uv tool install git+<repo-url>
pysql --help                 # now available everywhere
```

This is a **snapshot**. After changing the code, `uv tool install --force .` is
*not* enough to refresh it — the version stays `0.1.0`, so uv reuses the cached
wheel and ships stale code. Use `make install` (which adds `--no-cache`) to
rebuild, or `uv tool uninstall pysql` to remove it.

**Developing on pysql?** Install it **editable** so the `pysql` command always
runs your current source — no rebuilds, no cache staleness:

```bash
make dev                     # = uv tool install --force --editable .
```

**2. A wheel for teammates** — build it once, install anywhere Python is present:

```bash
uv build                     # -> dist/pysql-<version>-py3-none-any.whl
uv tool install dist/pysql-*.whl     # (or: pipx install …, pip install …)
```

**3. A standalone binary** (no Python required on the target machine) — bundles
Python and libpq into a single file you can drop in `/usr/local/bin`:

```bash
./scripts/build-binary.sh                          # -> dist/pysql  (~22 MB)
sudo install -m 0755 dist/pysql /usr/local/bin/pysql
pysql --help
```

The binary is built for the OS/arch/libc of the machine you build on, so build on
one that matches your targets (e.g. a glibc Linux box for glibc Linux users). RDS
IAM auth (boto3) is excluded to keep it lean — use method 1 or 2 if you need it.

## In the prompt

The prompt shows the database you're connected to, e.g. `(mydb) pysql>`.

Press `Tab` to complete. It's context-aware:

- after `\c`/`\l` → database names; after `\d` → table names
- a partial `\command` → the meta-command
- in SQL, right after `FROM`/`JOIN`/`INTO`/`UPDATE`/`REFERENCES` → table names;
  elsewhere → column names, scoped to the tables named in the statement
- `alias.`/`table.` → columns of just that table (aliases from `FROM x AS a` are resolved)

SQL keywords are always offered too. The name lists refresh on connect and on
every `\c`.

Press `Ctrl-R` for a full-screen **fuzzy history search** (fzf-style): type to
filter past commands, `↑`/`↓` to choose, `Enter` to drop the command onto the
prompt (editable), `Esc` to cancel — a friendlier replacement for readline's
`(reverse-i-search)`. `PgUp`/`PgDn` (like `↑`/`↓`) step through history inline.

### Vi edit mode

`\v` toggles **Vi editing** at the prompt (or set `vi = on` in `~/.pysqlrc` to
start in it — see [Settings](#settings)). You then get the full Vi key set from
`prompt_toolkit`: `Esc` for NORMAL mode, `w`/`e`/`b` to move by word, `a`/`i`/`o`
to insert, `dw`/`cw`/`dd`/`x` to edit, counts (`3dw`), `.` to repeat, visual
mode, and so on. A `-- NORMAL --` / `-- INSERT --` tag on the right shows the
current mode.

In NORMAL mode, `:w` (or `:wq`) runs the current line and `:q` clears it — note
these are *blind* (there's no echoed `:` command line, so nothing shows as you
type them; the mode tag is your cue). Plain `Enter` runs the line too, so `:w`
is really just muscle memory. Quitting pysql itself is still `\q` or `Ctrl-D`.

- End statements with `;`. Multi-line statements accumulate until then, and
  semicolons inside strings, comments, and `$$`/`$tag$` bodies (e.g. a plpgsql
  function) don't count — so you can type or paste a function definition, or
  several statements on one line.
- `EXPLAIN` / `EXPLAIN ANALYSE` — rendered as a plan tree with flame-graph-style
  bars sized by each node's **self** cost/time (its own share, children
  subtracted), so the expensive node stands out. Row-estimate misses (≥10× off)
  and disk reads are flagged, and the hottest node is called out. Works with the
  British spelling and with extra options (`EXPLAIN (ANALYZE, BUFFERS) …`); an
  explicit non-JSON `FORMAT` is passed through untouched.
- `\d` — list all tables in the current database (alphabetical), with an
  estimated size (from catalog stats, so the listing stays instant and never
  blocks on a locked table) and a partition count for partitioned tables.
  Partition child tables are hidden (their parent is marked instead).
- `\d <table>` — describe one table, kept short: columns (types, nullability,
  defaults) plus **partition** information — for a partitioned table its key and
  child partitions (with bounds and sizes), for a partition its parent and
  bounds.
- `\d+ <table>` — the **full** description: everything in `\d`, plus sizes,
  indexes, constraints (foreign keys / checks), and activity stats. Each index
  shows how often it's actually been used (`idx_scan`), colour-coded green
  (frequent) → red (unused) so dead indexes stand out — unused primary-key /
  unique indexes are kept amber, since they're needed for integrity, not reads.
- `\dn` — list schemas (system ones hidden) with owner and object count, plus a
  `search_path` header; the **current** schema (`current_schema()`) is marked `→`
  and highlighted.
- `\dT <table>` — print a reconstructed **`CREATE TABLE`** statement for a table:
  columns (type, identity, generated, default, `NOT NULL`), the table constraints
  (primary key / unique / foreign key / check), and any remaining indexes as
  `CREATE INDEX` lines. Postgres has no built-in table-DDL function, so this is
  assembled from the catalog (`pg_get_constraintdef` / `pg_get_indexdef` do the
  heavy lifting) and printed **plain** so it pastes cleanly. It's a *structural*
  reconstruction, not a full `pg_dump` — no ownership, grants, storage params,
  triggers or comments (and an old-style `serial` column shows its `DEFAULT
  nextval(…)` but not a separate `CREATE SEQUENCE`). The table name Tab-completes.
- `\ds` — list sequences (schema, name, type, owner, last value). `\ds <name>`
  shows one sequence's full state — start/min/max/increment/cycle/cache, its
  current **last value** and `is_called`, the **next value** it would hand out,
  and the table column it backs (for `SERIAL`/identity). Viewing is **read-only**:
  it reads `pg_sequences` and the sequence relation, and *never* calls `nextval`,
  so it never advances the sequence. The name Tab-completes; give a partial or
  wrong name and you get a fuzzy list of matches to pick from (and `schema.name`
  disambiguates a name used in several schemas).
- `\ds <name> <value>` — **set** a sequence's value (via `setval`). This *does*
  change the sequence, so it asks to confirm, showing the current value, the new
  value, and the value the next `nextval()` will return (`value + increment`).
  Out-of-range values and non-integers are rejected, and setting below the current
  maximum of a key column is exactly the footgun the confirmation guards against.
- `\dx` — list installed extensions (name, version, schema, description).
- `\l` — list all databases (alphabetical), with owner and size
- `\l <database>` — stats for one database (size, connections, cache hit %, activity)
- `\du` — roles and groups, the sane way: a table of users vs groups with their
  attributes spelled out and the groups they belong to, plus a **membership
  tree** so the hierarchy (and `WITH ADMIN OPTION`, shown as `*`) is obvious.
  Built-in `pg_*` roles stay out of the table but still show as memberships.
- `\du <role>` — one role in detail: attributes, group memberships, its own
  members, inheritance, connection limit, and password expiry
- `\dp` — an **object-access map**: schemas and tables (rows) against roles
  (columns), cells showing *effective* access — Postgres resolves group
  membership, inheritance, `PUBLIC` and ownership for us, so this answers "who
  can actually touch what", not just who was granted what. Superusers bypass all
  checks and are noted, not shown.
- `\dp <table>` — privileges on one table, spelled out, with a `via` column that
  says *how* each role's access arrives (owner / direct / via a group), plus
  `PUBLIC`
- `\dp <role>` — everything one role can reach: schema `USAGE`/`CREATE` and its
  per-table privileges
- `\locks` — current table locks: which table, the lock mode, whether it's
  **held** (green) or **waiting** (red), the backend's pid/**user**/state/duration,
  the query, and — for waiters — which pid is **blocking** them
  (`pg_blocking_pids`). Great for spotting an `idle in transaction` holder
  stalling everyone else.
- `\top` — a snapshot of server activity from `pg_stat_activity`: a summary line
  (`N connections · A active · I idle · T idle in txn (max …)`) plus a table of
  the busy backends (pid, user, database, state, how long in that state, what
  they're waiting on, and the query), active-first then longest-running. Quickly
  shows long-running queries and `idle in transaction` backends.
- `\top <secs>` — a live, full-screen **monitor** that refreshes every `<secs>`
  seconds, with a **heatmap** of load over time above the backend table — three
  rows (active queries, blocked, idle-in-transaction), each cell coloured
  green→yellow→red by intensity. Press `q` (or Esc / Ctrl-C) to exit.
- `\cancel <pid>` — ask a backend to stop its **current query** (`pg_cancel_backend`);
  the connection stays open. `\kill <pid>` — **terminate** the whole backend
  (`pg_terminate_backend`), rolling back its transaction and dropping the
  connection. Both show the target (user / database / state / query) and ask for
  confirmation first; `\cancel` warns if the backend is idle (nothing to cancel —
  use `\kill`). Pair them with `\top` / `\locks` to deal with a runaway query or
  a stuck `idle in transaction` holder.
- `\kill <user>` — terminate **all** of a user's backends; `\killdb <database>` —
  terminate all backends on a database (handy before `DROP DATABASE`). Both list
  the backends they'll hit and confirm first, never touch your own session, and
  report how many were terminated (per-backend permission errors are shown, since
  a non-superuser can only signal its own role's backends or with
  `pg_signal_backend`).
- `\params` — cluster parameters (`pg_settings`) with human-readable values
  (`128MB`, not `16384`), where each is set from (`source`), and whether changing
  it needs a restart (`context`). Values changed from their default are
  highlighted, so you can see at a glance what's been tuned. `\params <filter>`
  narrows to parameters whose name contains `<filter>` (e.g. `\params mem`).
- `\export (select …)` — export query results to a file. The **format** is
  `csv` (default), `tsv`, `json`, or `jsonl`, chosen by the file extension
  (`to data.json`) or an explicit `as <format>` (`\export (select …) to data.txt
  as tsv`). With no filename it writes a timestamped file with the right
  extension. csv/tsv stream server-side via `COPY … HEADER` (quoting/NULLs
  handled); `json` is an array of row objects, `jsonl` is one object per line
  (streamed, best for large results). A failed query never leaves a
  half-written file behind.
- `\import <file> [to <table>]` — load a file into an **existing** table (the
  counterpart to `\export`). Format is inferred from the extension or set with
  `as <format>` (csv/tsv/json/jsonl); the table name defaults to the file's name
  if you omit `to <table>`. csv/tsv stream via `COPY … FROM STDIN` and match
  columns **by header name** (so column order/subsets are fine); json/jsonl map
  each object's keys to columns. The load is atomic — on any error nothing is
  imported.
- `\f <file>` (or `\file <file>`) — run the SQL statements in a file. Uses the
  same quote/dollar-tag-aware splitter as the prompt (so function bodies and
  multi-statement lines are handled), runs each statement, and prints a summary
  (`N statement(s) from <file> — M failed`). A final statement without a trailing
  `;` still runs. By default it continues past errors; add a trailing **`stop`**
  (`\f <file> stop`) to halt at the first error instead — handy for migrations.
- `\cat <file>` — print a file from the filesystem (paged if it's long), without
  leaving pysql — handy for glancing at a `.sql` file before `\f`-ing it. The
  filename **Tab-completes** (empty + Tab lists the current directory; `src/` +
  Tab lists inside it; `~/` expands to home). Path completion also works for the
  first argument of `\f` / `\file` / `\import`.
- `\watch <query> [interval [count]]` — re-run a query on a timer. `interval` is
  seconds (default `2`, decimals allowed) and `count` is how many times to run
  (default: until you press **Ctrl-C**). Each run is printed under a header with a
  timestamp; paging is off for the duration. Example — every 5 seconds, 5 times:

  ```
  pysql> \watch select now(); 5 5
  ```

  The trailing numbers are peeled off the end of the line, so a query that itself
  ends in a number should be terminated with `;` to keep them apart
  (`\watch select 1; 5`).
- `\s` — **saved queries**. `\s` on its own lists them; `\s <name>` recalls the one
  named onto your prompt (filling any binds first) so you can run it; `\s <name>
  <query>` saves (or updates) a query under that name. `\sD <name>` deletes one.
  They live in `~/.pysql_queries.yaml` (managed for you). A query can embed `{bind}`
  placeholders — see [Saved queries](#saved-queries) for the interactive bind form.
- `\c <database>` — connect to another database; `\c -` reconnects to the previous one
- `\C` — list saved connection **profiles**, grouped **Connected** (open sessions,
  the current one marked `→`) then **Not connected**, with any live SSM tunnel shown
  on its own line underneath so long lines don't wrap. `\C <name>` opens a profile in
  its own session (or switches to it if already open); `\C -` switches back to the
  previous profile. If a profile has no stored password and the server asks for one,
  pysql prompts you for it, connects, and saves it (encrypted) back into the profile
  so you're not asked again. See [Connection profiles](#connection-profiles).
- `\add <name>` — save the **current** connection (host, port, database, user,
  and password — encrypted) as a new profile, so you don't have to hand-edit the
  file or run `\enc` yourself. On an RDS IAM connection it instead prompts for
  the AWS fields and writes an `auth = rds-iam` profile (the token is never
  stored) — see [RDS IAM authentication](#rds-iam-authentication)
- `\close` — close the current profile's session (and fall back to the previous one)
- `\w` — toggle the current session between its **reader and writer** endpoint (for
  profiles with a `reader:` host; a red `*` in the prompt means you're on the writer)
  — see [Reader / writer endpoints](#reader--writer-endpoints)
- `\P <port>` — pin the current session's **SSM tunnel** to a fixed local port (`0`
  picks a free one) and reconnect, so other apps can reach the same database at
  `localhost:<port>` — see [Bastion tunnels](#bastion-tunnels-aws-ssm)
- `\run-on-active <query>` — run a query on **every open session** at once
  (everything `\C` has opened, plus the startup connection). By default pysql
  asks before each database, so you can pick which ones to hit:

  ```
  pysql> \run-on-active alter table orders add column note text;
  Run on prod   (app@mydb1.example/shop) [y/N/a/q]: y
  Run on stage  (app@mydb2.example/shop) [y/N/a/q]: a    # a = yes to all the rest
  ```

  At the prompt `y` runs it, a blank line (the default) skips that database, `a`
  runs it on all the remaining sessions without asking again, and `q` stops.
  Turn the per-database confirmation off with `\prompt` (below) and it runs on
  everything without asking — handy for a read-only query, but think twice before
  disabling it for writes. The query can hold several statements (`;`-separated);
  each runs on each session.
- `\prompt on` / `\prompt off` (or just `\prompt` to toggle) — whether
  `\run-on-active` confirms each database before running. On by default; persists
  with `\save`.
- `\enc <password>` — print the encrypted string to paste into a profile file
- `\conninfo` — show the current connection
- `\timing` — toggle query timing (on by default)
- `\pager on` / `\pager off` — result paging (on by default, or set your own
  default in [`~/.pysqlrc`](#settings)); long or wide results go through `$PAGER`
  (default `less -SRX`, so wide tables scroll sideways). Only pages when output
  doesn't fit the terminal.
- `\color on` / `\color off` (or just `\color` to toggle) — turn all coloured
  output on/off for the session, including the [zebra row banding](#settings)
- `\v` — toggle [Vi edit mode](#vi-edit-mode) at the prompt (off by default; set
  `vi = on` in `~/.pysqlrc` to start in it)
- `\save` — write the current `pager`/`timing`/`vi`/`prompt`/`display_mode`
  settings to `~/.pysqlrc`
- `\?` — list meta-commands
- `\q` or Ctrl-D — quit; Ctrl-C clears the current statement

## Connection profiles

Save connections you use often in `~/.pysql_profiles.yaml` (YAML). Each top-level
key is a profile:

```yaml
engineering-test-db:
  username: myuser
  password: gAAAAABk...Qm3w==      # an \enc token (see below)
  host: my-database.host.name.com
  port: 5432
  database: default
```

> **Format note.** YAML is the preferred format. The older INI file
> `~/.pysql_profiles` still works and is read automatically **when no YAML file
> is present**, so existing setups keep running untouched — nothing to migrate.
> When both exist, the YAML file wins. (`host` and `hostname` are both accepted.)

The quickest way to create one is to connect however you normally would and then
save it:

```
uv run pysql "postgresql://myuser:mysecret@my-database.host.name.com:5432/default"
pysql> \add engineering-test-db     # appends the profile above, password encrypted
```

`\add` appends to whichever profile file is active (creating `~/.pysql_profiles.yaml`
if you don't have one yet) and preserves the rest of the file, comments and all.

Or write the file by hand. The `password` is an **encrypted** string, not
plaintext — generate it with `\enc` (in-session) or `pysql --enc` (from the shell):

```
pysql> \enc mysecretpassword
gAAAAABk...Qm3w==

$ pysql --enc mysecretpassword     # same token, without opening a session
gAAAAABk...Qm3w==
```

Use that value as the profile's `password`. You can also **skip the password
entirely** when writing a profile by hand: the first time you `\C` into it and the
server asks for one, pysql prompts you, connects, and writes the password back into
that profile (encrypted, in place — your other profiles and comments are left
untouched), so it's there next time. A blank answer at that prompt just connects
without a password (for `trust`/`peer`/`.pgpass` auth). Either way, connect by name:

```
pysql> \C engineering-test-db     # open it in a new session
pysql> \C                           # list profiles: Connected first, then Not connected
pysql> \C -                         # switch back to the previous profile
pysql> \close                       # close the current profile's session
```

You can also launch straight into a profile: `pysql @engineering-test-db`.
Several profiles can be open at once; `\C` switches between them and the prompt
shows the active profile name.

### Reader / writer endpoints

A profile can name a separate **reader** endpoint alongside the writer `host:`.
When it does, pysql connects to the **reader by default** (a safe landing spot)
and `\w` toggles to the writer and back:

```yaml
core:
  username: myuser
  password: gAAAA...
  host: writer.cluster.example.com     # the writer endpoint
  reader: reader.cluster.example.com   # connects here by default
  port: 5432
  database: mydb
  # default_mode: writer               # optional — land on the writer instead
```

```
core (mydb) pysql>      \w        # now on the writer
core (mydb) pysql*>               # red * = you're on the writer
```

- The prompt shows a red `*` right after `pysql` whenever you're on the writer, so
  the primary is never a surprise. The reader shows no marker.
- `default_mode: writer` makes the profile land on the writer at connect time; omit
  it (or set `reader`) to default to the reader.
- Works with **RDS IAM** auth (the token is signed for whichever endpoint is active)
  and through **[SSM tunnels](#bastion-tunnels-aws-ssm)** — `\w` restarts the forward
  onto the other endpoint and reconnects, keeping the same local port.
- `\conninfo` tells you which endpoint you're on. A profile with no `reader:` has no
  writer/reader distinction, so `\w` reports there's nothing to toggle.

**On the encryption (read this):** `\enc` uses a key stored in `~/.pysql_key`
(created on first use, `chmod 600`), so pysql can decrypt the password
automatically at connect time. This keeps plaintext out of the profile file and
protects it if it's copied to another machine — but it is **at-rest obfuscation,
not a vault**: anyone who can read *both* the profile file and `~/.pysql_key`
on your machine can recover the password. Keep both files private (`\add` already
`chmod 600`s the profile file). `\enc` lines are never written to shell history.

**On a fresh machine:** there's no setup step — the key is created automatically.
The first time anything touches encryption (your first `\enc` / `pysql --enc`, or
the first connect to a profile with a stored password), pysql generates
`~/.pysql_key` with a random key and `chmod 600`. But because each machine gets
its **own** random key, a token is only decryptable by the key that made it. So
when you move to a new machine you have two choices:

- **Re-encrypt there** (simplest): run `pysql --enc` on the new machine and paste
  the fresh tokens into its profile file. The key is created for you on the spot.
- **Or copy `~/.pysql_key`** over alongside the profile file (via a secure channel,
  keeping it `chmod 600`) so the existing tokens keep working.

If you copy a profile file but not the key, connecting fails with a clear message
(`could not decrypt password — the key in ~/.pysql_key may have changed, or the
value isn't a \enc token`) — pysql generates a *new* key, which can't decrypt
tokens made with the old one.

### RDS IAM authentication

For Amazon RDS/Aurora with IAM auth, set `auth: rds-iam` instead of a password.
pysql generates a short-lived auth token at connect time (so nothing secret is
stored — not even an encrypted password) and enforces TLS:

```yaml
engineering-db-iam:
  auth: rds-iam
  username: engineering_user
  host: mycluster.cluster-xxxx.ap-southeast-2.rds.amazonaws.com
  port: 5432
  database: mydb
  region: ap-southeast-2
  aws_profile: engineering       # optional — omit to use the default AWS credentials
  sslmode: require         # optional — defaults to require for rds-iam
```

Then `\C engineering-db-iam` (or `pysql @engineering-db-iam`) connects. The token is
generated with the equivalent of:

```bash
aws rds generate-db-auth-token --hostname … --port … --region … --username … [--profile …]
```

but in-process via **boto3**, which is an optional dependency — install it with
`pip install "pysql[rds]"` (or `uv sync --extra rds`). Your AWS credentials come
from the named `aws_profile` (your `~/.aws/…`) or the default credential chain;
pysql stores none of them. A fresh token is generated on every connect —
including a `\c` database switch — so it never goes stale.

`\add` is IAM-aware: if the current connection is an IAM one (pysql recognises
the signed token), `\add <name>` won't try to save the short-lived token as a
password. Instead it prompts for the few things it can't infer — AWS region
(pre-filled from the RDS endpoint), AWS profile, and `sslmode` — and writes a
proper `auth: rds-iam` profile like the one above. So you can launch with a
token-based conninfo and `\add` it as a reusable profile.

### Bastion tunnels (AWS SSM)

For a private RDS/Aurora cluster reached through a bastion, add a `tunnel:` block
and pysql brings up an **SSM port-forwarding** session automatically when you open
the profile — no SSH keys, no open ports:

```yaml
default:
  username: myuser
  database: my_production_database
  # password OR auth: rds-iam — both work through the tunnel
  password: gAAAA…
  tunnel:
    via: ssm                              # the only backend today (pluggable)
    bastion: i-0abc123                    # SSM target: an instance-id, tag:Name=bastion,
                                          #   or several tags (all must match): tag:env=prod,role=bastion
    host: engineering-host.cluster-xxx.ap-southeast-2.rds.amazonaws.com   # the private endpoint
    remote_port: 5432                     # defaults to the profile port, else 5432
    local_port: 0                         # 0 / omitted = pick a free local port
    region: ap-southeast-2                # falls back to the profile's region
    aws_profile: engineering                    # falls back to the profile's aws_profile
```

`\C default` (or `pysql @default`) starts the tunnel, waits for the local port,
then connects to it; `\close` / quit tears it down. `\C` shows which profiles
tunnel and, for open ones, the live tunnel and its local port:

```
→ default   myuser@…/mydb  (current)  ⇄ ssm i-0abc123 → engineering-host…:5432  (localhost:51234)
· staging   myuser@…/mydb  ⇄ ssm
```

By default the tunnel picks a free local port (`local_port: 0`). To put it on a
**fixed** port so other apps can reach the same database, use `\P <port>`:

```
pysql> \P 5555
Tunnel now on 127.0.0.1:5555  — point your apps at localhost:5555 (ssm i-0abc123 → engineering-host…:5432)
```

`\P` closes and re-opens the forward on the chosen port (the new listener comes up
and is verified before the old one is dropped, so a busy port leaves your session
untouched), then reconnects — handy for pointing a GUI client or a local service at
the database you're on. Pass `\P 0` to move back to an auto-picked port. The pinned
port survives a `\w` reader/writer toggle.

It **composes with RDS IAM** — set both `auth: rds-iam` and a `tunnel:` and you
connect through the bastion with a per-connect token and no stored password (the
token is signed for the real RDS endpoint even though the socket goes to
localhost). Requirements: the **AWS CLI** and its **`session-manager-plugin`**
installed locally (pysql checks and tells you if either is missing); the bastion
must be an SSM-managed instance your IAM identity can `StartSession` on. SSH-based
bastions aren't supported yet — `tunnel.via` is the extension point for them.

## Saved queries

Keep queries you run often under a short name, and run them with `\s <name>`:

```
pysql> \s sku_by_id select * from sku where id = '{1}'   # save
Saved query sku_by_id  (binds: {1})
  run it with \s sku_by_id

pysql> \s                                                # list them
pysql> \s sku_by_id                                      # run it
pysql> \sD sku_by_id                                     # delete it
```

- `\s <name> <query>` saves (or updates); the query is everything after the name,
  so spaces, quotes and `;` are kept verbatim. Multi-statement queries are fine —
  each statement runs in turn.
- `\s <name>` runs the saved query; `\s` on its own lists them (with a preview and
  the binds each one needs); `\sD <name>` deletes one.
- They're stored in `~/.pysql_queries.yaml`, managed by pysql (so it's rewritten
  on save/delete). Multi-line queries are written as readable `|` blocks, so the
  file stays easy to eyeball.

**Binds.** A query may contain `{name}` placeholders — `{1}`, `{since}`, anything
matching `{word}`. When you run it, pysql pops up a small form with one field per
placeholder. The **full query is shown above the fields with each `{placeholder}`
highlighted**, so you can see exactly what each one fills in (handy for positional
binds like `{1}`, `{2}`):

```
pysql> \s orders_since select * from orders where created_at > '{since}' and region = '{region}'
pysql> \s orders_since
  Binds for \s orders_since

  select * from orders where created_at > '{since}' and region = '{region}'
  Tab/⇧Tab or ↓/↑ move · Enter → paste to prompt · Esc cancel
  ──────────
   since   2026-01-01
  region   EU
```

**Tab** (or ↓) moves to the next field, **Shift-Tab** (or ↑) to the previous,
**Esc** cancels. Each value is substituted **literally** for its placeholder — the
template owns the quoting, which is why you write `'{since}'` (quoted) for a
text/date value and `{id}` (bare) where you want a raw number. So `id = '{1}'` with
`1` → `42` becomes `id = '42'`. (Placeholders are only `{word}`; Postgres literals
like `'{1,2,3}'::int[]` or `'{"a":1}'::jsonb` are left alone.)

**Enter doesn't run it — it pastes it.** Running a saved query (binds or not) drops
the finished SQL onto your prompt rather than executing it, so you get a last look
(and a chance to edit) before pressing Enter to run — or **Ctrl-C** to discard it.
This way a destructive saved query is never fired off by accident. Because
substitution is literal text, also treat saved queries like any SQL you write by
hand — don't paste untrusted input into a bind for a query you didn't author.

## Settings

Put preferences in `~/.pysqlrc` to change the startup defaults:

```ini
[settings]
pager             = off     ; don't page results by default
timing            = on      ; show query timing (the default)
vi                = on      ; start in Vi edit mode (off by default)
confirm_broadcast = on      ; \run-on-active asks before each database (the default)
display_mode      = color   ; "plain" turns all colour off (default: on if a TTY)
row_color_1       = #1e1e2e ; alternating "zebra" band behind query-result rows
row_color_2       = #313244 ; (column headers are shown bold)
```

The on/off values accept `on`/`off` (also `true`/`false`, `yes`/`no`, `1`/`0`).
These seed the session; `\pager`, `\timing`, `\v` and `\prompt` still toggle them
for the current session. Unknown keys and typos are ignored, so a bad line never stops
pysql from starting. Inline `; comments` after a value are fine (but use `;`, not
`#` — a value may *be* a `#hex` colour).

**Colours.** `display_mode = plain` switches off *all* colour (handy for logs or
a mono terminal); `color` forces it on even when output is redirected. Left
unset, colour follows whether you're on a terminal — and `\color on`/`\color off`
(or just `\color`) flips it live during a session. `row_color_1` and
`row_color_2` are two `#hex` colours (3- or 6-digit; `#rgb` or `#rrggbb`) painted
as full-width background bands behind alternating query-result rows, with the
text sitting on the band and the column headers in bold. Both colours must be
set (and colour on) for banding to appear. British spellings work too
(`display_mode = colour`, `row_colour_1`). The two row colours are read once at
startup — edit the file to change them.

You don't have to edit the toggles by hand: set them with `\pager`, `\timing`,
`\v`, `\prompt` and `\color`, then run `\save` to persist them (it writes
`display_mode` from `\color`'s current state). `\save` updates the values in place, leaving your
comments and any other content in the file untouched.

## Roadmap sketch

Done:

- Quote- and dollar-tag-aware statement splitting (handles function bodies,
  strings/comments, and multiple statements per line)
- Context-aware Tab completion for meta-commands, databases, tables, and columns
- Configurable result styling: bold headers, two-colour zebra row banding, and a
  `display_mode = plain/color` switch (all from `~/.pysqlrc`)
- Richer-than-psql introspection for a single table (`\d <table>`) and database
  (`\l <database>`)
- Flame-graph-style `EXPLAIN` / `EXPLAIN ANALYSE` rendering (self-time bars,
  row-estimate flags, hotspot call-out)
- Intuitive roles/groups view (`\du`): users vs groups, attributes in plain
  words, and a membership tree
- Effective object-privilege views (`\dp`): access map, per-table `via`
  breakdown, and per-role reach — membership/inheritance/PUBLIC resolved
- Fuzzy history search on `Ctrl-R` (fzf-style full-screen picker); `PgUp`/`PgDn`
  step through history
- Vi edit mode (`\v`, or `vi = on`): full Vi motions/operators at the prompt,
  `:w`/`:q`, and a `-- NORMAL/INSERT --` indicator
- Saved connection profiles (**YAML**, with legacy INI still read) with encrypted
  passwords (`\C` / `\add` / `\enc`) or **RDS IAM** auth (`auth: rds-iam`, token
  per-connect), auto-managed **AWS SSM bastion tunnels** (`tunnel: {via: ssm, …}`,
  shown in `\C`), multiple open sessions at once, and `pysql @profile` launch
- `~/.pysqlrc` for persistent settings (e.g. default the pager off)
- `\locks` — live table-lock view with holders, waiters, and blocking pids
- `\dn` — list schemas, with the current one highlighted
- `\dT <table>` — print a reconstructed `CREATE TABLE` statement (columns,
  constraints, indexes)
- `\ds` / `\ds <name>` — list sequences, or view one's value read-only (never
  advances it), with fuzzy name matching; `\ds <name> <value>` sets it (confirmed)
- `\dx` — list installed extensions
- `\top` / `\top <secs>` — backend-activity snapshot, or a live monitor with a
  load heatmap
- `\cancel <pid>` / `\kill <pid|user>` / `\killdb <mydb>` — cancel a query or
  terminate backends (one, all of a user's, or all on a database), with confirmation
- `\params [filter]` — cluster parameters with readable values, source, and the
  non-default ones highlighted
- `\export (select …)` — export query results to csv / tsv / json / jsonl
- `\import <file> [to table]` — load csv / tsv / json / jsonl into a table
- `\f <file>` / `\file` — run a SQL script from disk

Next:

- `\d`-style introspection for other objects (views, indexes, functions, schemas)
- Qualified-name completion across schemas; pull tables from subqueries/CTEs into scope
