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
| 6 | A `[client_features.{watch,verifier,receiver_channels}]` block per TUI surface you want | same file (+ matching parser module — see §5.1)      |
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

# Monitoring → Receiver Channels
# (sigmond loads your parser at TUI time and asks it to extract
# (status_dns, configured_freqs_hz, encoding_int) from your parsed
# per-instance config — see §5.1)
[client_features.receiver_channels]
description     = "<one line shown in dropdown>"
per_instance    = true                              # false → singleton client
parser_file     = "src/your_client/sigmond_tui.py"  # path relative to repo root
parser_attr     = "parse_receiver_channels"         # callable name in that file
# Singleton-only (per_instance = false):
singleton_label = "(singleton)"                     # suffix on dropdown label
config_path     = "/etc/your-client/your-config.toml"   # absolute config path
```

The loader (`lib/sigmond/client_features.py`) walks every
enabled+installed client's `deploy.toml` at TUI launch — no edits to
sigmond required.

### 5.1 Receiver Channels parser

If you declared `[client_features.receiver_channels]` above, ship a
matching parser module in your repo (the file at `parser_file`).  It
exports a pure function over a parsed TOML dict:

```python
# src/your_client/sigmond_tui.py
from typing import Optional

from sigmond.ka9q_encoding import ENCODING_INTS, encoding_to_int


def parse_receiver_channels(
    cfg: dict,
) -> tuple[str, set[int], Optional[int]]:
    """Return (status_dns, configured_freqs_hz, encoding_int).

    status_dns           — radiod mDNS status name (e.g. "bee1-status.local")
    configured_freqs_hz  — set of frequencies the client tunes
    encoding_int         — ka9q-radio Encoding int (1=s16le, 2=s16be,
                           4=f32, 8=f32be) or None to match any encoding
    """
    ...
```

Sigmond imports the module by file path (`importlib.util.spec_from_
file_location`), so it does NOT need to be importable by package name
from sigmond's venv — only the on-disk file needs to resolve.  Your
parser runs inside sigmond's Python runtime, so it can
`from sigmond.ka9q_encoding import ...` for the shared encoding
lookup table (and any other sigmond utility module).

See the five committed parsers under
`/opt/git/sigmond/{psk,wspr,hfdl}-recorder/{src,}/<pkg>/sigmond_tui.py`,
`/opt/git/sigmond/codar-sounder/src/codar_sounder/sigmond_tui.py`,
`/opt/git/sigmond/hf-timestd/src/hf_timestd/sigmond_tui.py`,
`/opt/git/sigmond/hf-gps-tec/src/hf_gps_tec/sigmond_tui.py` for
working examples covering the full range of config shapes
(multi-radiod, band-name lookup table, per-transmitter array,
per-channel-group hierarchy, singleton config).

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

One-shot lint of every drop-in surface:

```bash
smd admin diag drop-in your-client
```

Walks repo presence → `deploy.toml` → contract subcommands →
`[client_features]` → catalog + topology → end-to-end
ContractAdapter view, prints green/yellow/red per check, exits
non-zero if anything is red.  Use this after each edit to the
client repo to confirm sigmond still picks it up everywhere.

The individual probes, if you want to walk them by hand:

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
isn't exiting 0 on the operator's UID — re-read §4's operator-
callable rule (`smd admin diag drop-in` catches this and points you at
the fix).

---

## 8. Author `config/help.toml` (three-tier audit)

Sigmond's in-TUI Textual config wizard reads `config/help.toml`
sidecar files to drive per-field labels, examples, validator
hints, and focus-driven help bodies.  Authoring this file well is
what turns "raw TOML editor in a modal" into "guided wizard."

**Three-tier classification for every field you put in the config
TOML:**

| Tier | Examples                                                | Action |
|------|---------------------------------------------------------|--------|
| 1    | install-canonical paths (`dumphfdl`, `/var/lib/...`),  | mark `hidden = true` |
|      | FHS dirs, protocol constants (sample rate, blocktime), |        |
|      | PSWS endpoint hostnames, radiod-internal tunables      |        |
| 2    | host-level operator knobs (callsign, grid_square,      | author full help body |
|      | mDNS host, sink opt-ins, antenna tuning gain)          |        |
| 3    | per-instance differentiators (`reporter_id`,           | wizard-locked by sigmond |
|      | `instance.metadata`)                                   | — no help.toml entry needed |

For multi-instance clients tier 3 is real; for singletons
(hf-timestd, mag-recorder) tiers 2 and 3 collapse — the whole
config is host-level.

**Schema** (per-key subtable):

```toml
[<section>.<key>]
title          = "Short label (≤40 chars)"
help           = """Multi-line operator-facing body.
Explain when to change it and what the implications are."""
example        = "what a valid value looks like"
validator_hint = "one-line constraint summary"
required       = true       # tier-2 must-fills
hidden         = true       # tier-1 invariants — wizard skips entirely
```

Setting `hidden = true` keeps the field in the on-disk TOML
(`config apply` uses deep-merge, so untouched keys survive) but
the wizard never surfaces it.  Emergency override remains
available via `smd config edit <client>`.

**Quick audit workflow:**

```bash
# 1. dump every field the wizard would surface today
<client> config show --json | jq 'keys[]'

# 2. for each field, ask "tier 1, 2, or 3?"
#    tier 1 → add `[<section>.<key>] hidden = true` to help.toml
#    tier 2 → author title / help / example / validator_hint
#    tier 3 → handled by sigmond directly (no entry)

# 3. launch the wizard and confirm only tier-2 fields appear
smd tui   # Installation → ⚙ Configuration → Edit selected
```

**Living examples (2026-05-29 audit pass — all five client repos):**

* `psk-recorder/config/help.toml` — the canonical reference,
  three-tier audit applied (paths × 6 hidden, station/decoder_kind
  surfaced).
* `wspr-recorder/config/help.toml` — aggressive tier-1 hiding;
  WSPR-protocol constants (12 kHz / USB / float / 1300–1700 Hz
  passband) all hidden.
* `hf-timestd/config/help.toml` — singleton client, ~40 internal
  fields hidden, 17 operator knobs surfaced.

`feedback-config-invariants` in the auto-memory store records the
principle and why it matters.

---

## Reference: living examples

| Client          | Use it as a reference for…                                            |
|-----------------|-----------------------------------------------------------------------|
| `psk-recorder`  | a clean greenfield client; both `[client_features]` blocks present    |
| `hf-timestd`    | a singleton (one-per-host) client with `[client_features.verifier]`   |
| `hf-gps-tec`    | a per-instance client; canonical degraded-inventory implementation    |
| `mag-recorder`  | a non-radiod data-source client (§16)                                 |
