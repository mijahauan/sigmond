# Adding a new sigmond client

This is the checklist for someone **writing a new contract-conformant
client repo** (a recorder, analyzer, or anything else that wants to
appear in `smd list`, the TUI, and the sigmond pipeline).

This is intentionally short and opinionated.  Each step says exactly
what file to touch and points to [CLIENT-CONTRACT.md](CLIENT-CONTRACT.md)
for the rules behind it.  When in doubt, copy `psk-recorder/` — it's
the canonical reference for a v0.8-conformant client.

If you're _operating_ sigmond rather than authoring a client, you want
[install-quickstart.md](install-quickstart.md) instead.

---

## TL;DR — the seven things you must ship

| # | What                                                                                   | Where                                                |
|---|----------------------------------------------------------------------------------------|------------------------------------------------------|
| 1 | A repo at `/opt/git/sigmond/<your-client>/`                                            | git remote                                           |
| 2 | A `deploy.toml` manifest                                                               | `<repo>/deploy.toml`                                 |
| 3 | A templated systemd unit `(your-client)@.service`                                      | `<repo>/systemd/`                                    |
| 4 | Contract subcommands `version` / `inventory` / `validate` / `daemon` (+ JSON output)   | your CLI entry point                                 |
| 5 | A config template + default-config render step in `deploy.toml`                        | `<repo>/config/` + `[[install.steps]] kind=render`   |
| 6 | A `[client_features]` block in `deploy.toml` for any TUI surface you want             | same file                                            |
| 7 | (When ready) a catalog entry in sigmond's `etc/catalog.toml` or per-host override     | sigmond repo                                          |

After (1)–(6), `smd install <your-client>` and `smd tui` work.  After
(7), `smd list` shows you in the catalog and other hosts can pull you
in.

---

## 1. Repo layout

```
your-client/
├── deploy.toml                            # sigmond manifest (§5)
├── pyproject.toml                         # if Python
├── README.md
├── scripts/
│   └── install.sh                         # invoked by [build].steps
├── src/your_client/
│   ├── cli.py                             # argparse + subcommands
│   └── config.py                          # native config (§1)
├── config/
│   ├── your-client-config.toml.template   # rendered to /etc/ on install
│   └── help.toml                          # optional; per-key wizard labels
├── systemd/
│   └── your-client@.service               # templated; %i = instance
└── tests/
```

Use `/opt/git/sigmond/psk-recorder/` as your reference.

---

## 2. `deploy.toml` — the sigmond manifest

Minimum-viable shape.  See CLIENT-CONTRACT.md §5 for every field.

```toml
[package]
name             = "your-client"
version          = "0.1.0"
contract_version = "0.8"
description      = "<one line — shown in `smd list`>"
license          = "MIT"

[build]
steps = [
    "/opt/git/sigmond/your-client/scripts/install.sh --build-only",
]
produces = [
    "/opt/your-client/venv/bin/your-client",
]

[install]
[[install.steps]]
kind = "link"
src  = "/opt/your-client/venv/bin/your-client"
dst  = "/usr/local/bin/your-client"

[[install.steps]]
kind = "link"
src  = "systemd/your-client@.service"
dst  = "/etc/systemd/system/your-client@.service"

[[install.steps]]
kind      = "render"
src       = "config/your-client-config.toml.template"
dst       = "/etc/your-client/your-client-config.toml"
mode      = "0640"
owner     = "yourcli:yourcli"
if_absent = true

[[install.steps]]
kind  = "mkdir"
dst   = "/var/lib/your-client"
owner = "yourcli:yourcli"
mode  = "0750"

[systemd]
units = ["your-client@.service"]

[contract.config]
init = ["/usr/local/bin/your-client", "config", "init"]
edit = ["/usr/local/bin/your-client", "config", "edit"]
```

---

## 3. Systemd unit

Must be **templated** (use `@.service` and `%i` for instance).  See
[MULTI-INSTANCE-ARCHITECTURE.md](MULTI-INSTANCE-ARCHITECTURE.md) §3.

```ini
[Unit]
Description=Your client (%i)
After=network.target

[Service]
Type=simple
User=yourcli
ExecStart=/usr/local/bin/your-client daemon --instance %i
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## 4. Contract subcommands (CLIENT-CONTRACT.md §3)

Every client MUST implement these as `<your-client> <verb> --json`,
all printing JSON to stdout:

| Subcommand        | Exit-0 rule                                                                                  | Notes |
|-------------------|----------------------------------------------------------------------------------------------|-------|
| `version`         | always                                                                                       | basic metadata |
| `inventory`       | **always** — including when config can't be read (print a degraded payload with `issues`)    | sigmond's `installed` flag depends on this exiting 0 |
| `validate`        | exit nonzero when client is unhealthy                                                        | OK to exit 1 when config unreadable |
| `daemon`          | (no JSON; long-running)                                                                      | invoked from the systemd unit |
| `quality` (opt.)  | always                                                                                       | §17 — stream-quality snapshot |

**Operator-callable rule**: `version`, `inventory`, and the
config-show subcommand listed below MUST work for an unprivileged
operator.  Service-user-owned config files (mode 0640) are normal;
your `inventory` handler must catch `PermissionError` and
`FileNotFoundError` and print a contract-shaped degraded payload with
the failure as a `fail`-severity `issues` entry.  See
`hf-gps-tec/src/hf_gps_tec/cli.py` `_degraded_inventory_payload` for
the canonical implementation.

**Wizard subcommands (§14)** — required for the `smd config init|edit`
verbs and the in-TUI Textual wizard:

| Subcommand                          | Direction              | Purpose                                |
|-------------------------------------|------------------------|----------------------------------------|
| `config init [<instance>]`          | interactive            | run the whiptail wizard                |
| `config edit [<instance>]`          | interactive            | open `$EDITOR` on the config file     |
| `config show --json [--defaults]`   | stdout JSON            | machine-readable current config        |
| `config apply --json -`             | stdin JSON             | accept a (partial) dirty subset to save |

---

## 5. `[client_features]` — TUI registration (drop-in)

Add only what applies.  Omitting a sub-block hides your client from
that screen.

```toml
# Monitoring → Activity
[client_features.watch]
verb         = "your-client"   # defaults to the package name
description  = "<one line shown in dropdown>"
verbose      = true            # CLI accepts -v / --verbose
per_instance = true            # CLI accepts --instance REPORTER_ID

# Maintenance → Verifier
[client_features.verifier]
verb         = "your-client"
description  = "<what your audit does>"
kind         = "spot_queue"    # or "local_db"
per_instance = true
```

The loader (`lib/sigmond/client_features.py`) walks every
enabled+installed client's `deploy.toml` at TUI launch — no edits to
sigmond required.

---

## 6. Catalog entry (often optional)

The catalog (`sigmond/etc/catalog.toml`) is built from three layers:

  1. Auto-discovery from every installed `/opt/git/sigmond/*/deploy.toml`
  2. The repo-default catalog file (this one)
  3. `/etc/sigmond/catalog.toml` (per-host operator override)

If your client is at `/opt/git/sigmond/your-client/`, **layer 1
discovers it automatically** — no entry required for it to appear in
`smd list`, `smd install`, etc. on hosts that already have the repo.

You need a `[client.your-client]` block in the repo catalog only when:
- you want hosts that **haven't cloned the repo yet** to see it in
  `smd list --available` (so an operator can `smd install your-client`
  from scratch), OR
- you need to set fields that auto-discovery can't derive (e.g.
  `start_priority`, `topology_alias`).

```toml
[client.your-client]
kind            = "client"
description     = "<one line>"
repo            = "https://github.com/<org>/your-client.git"
uses            = ["ka9q-python"]                 # sibling libs you import
requires        = ["ka9q-python", "ka9q-radio"]   # must be enabled+installed
contract        = "0.8"
install_script  = "/opt/git/sigmond/your-client/scripts/install.sh"
```

For a private / in-development client, add the same block to
`/etc/sigmond/catalog.toml` instead (sparse overlay — only the
diverging keys).

---

## 7. Verify the drop-in

```bash
# operator-callable contract surfaces work without sudo
your-client version    --json
your-client inventory  --json      # MUST exit 0
your-client config show --json

# sigmond's view
smd config show          | grep your-client    # 'installed' should be yes
smd list                 | grep your-client    # lifecycle status
smd tui                                        # appears in Activity / Verifier
```

If `smd config show` reports your client as `not installed` but
`smd list` agrees it's installed, your `inventory --json` probably
isn't exiting 0 on the operator's UID — go re-read §4's operator-
callable rule.

---

## Reference: living examples

| Client          | Use it as a reference for…                                            |
|-----------------|-----------------------------------------------------------------------|
| `psk-recorder`  | a clean greenfield client; both `[client_features]` blocks present    |
| `hf-timestd`    | a singleton (one-per-host) client with `[client_features.verifier]`   |
| `hf-gps-tec`    | a per-instance client; canonical degraded-inventory implementation    |
| `mag-recorder`  | a non-radiod data-source client (§16)                                 |
