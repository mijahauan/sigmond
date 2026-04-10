# HamSCI Client Contract

**Version:** 0.1 (draft — Phase 1)
**Status:** Draft for review. Phase 2 retrofit of `hf-timestd` and
`wsprdaemon-client` will drive the first round of revisions.

## What this is

A spec every HamSCI client should conform to so that:

1. It runs **standalone** with no sigmond present, using only its own
   config file.
2. It can be **coordinated** under sigmond when sigmond is present,
   without any bespoke adapter code on the sigmond side.
3. New HamSCI clients (e.g. `psk-recorder`) can be developed against
   the contract from day one, and be integrated under sigmond by
   adding a single `[[clients.<name>]]` entry to
   `/etc/sigmond/coordination.toml`.

Sigmond is the *coordinator*; each client is an *independent peer*.
Sigmond never writes inside a client's config file. When sigmond needs
to tell a client something, it does so through
`/etc/sigmond/coordination.env` (KEY=VALUE, systemd
`EnvironmentFile=-` compatible) and per-unit systemd drop-ins in the
client's own `<unit>.d/` namespace.

## Contract surfaces

### 1. Native config

- Lives at `/etc/<client-name>/<client-name>.toml` (TOML).
- Multi-instance clients use `/etc/<client-name>/instances/<name>.toml`,
  one file per instance.
- Schema is owned by the client.
- The client's config contains only the knobs that make sense in
  isolation. Cross-client or cross-station concerns (CPU budget,
  per-radiod routing, station call/grid) come from sigmond.

### 2. Binding to radiod by id

- Each instance's config names its upstream radiod by **radiod id**,
  e.g.:
  ```toml
  [ka9q]
  radiod_id     = "k3lr-rx888"
  # Optional standalone fallback used when sigmond is absent:
  radiod_status = "k3lr-rx888-status.local"
  ```
- When running under sigmond, the client reads
  `/etc/sigmond/coordination.env` to resolve the id to a status DNS
  name, sample rate, and other per-radiod facts. Typical
  environment lookups:
  ```
  RADIOD_K3LR_RX888_STATUS
  RADIOD_K3LR_RX888_SAMPRATE
  ```
- When running standalone, the client uses its own `radiod_status`
  fallback field. It **must** work without sigmond present.

### 3. Self-describe CLI

Two subcommands are mandatory. Both emit JSON to stdout and exit 0 on
success.

**`<client> inventory --json`** — print the client's resource view per
instance. Shape:

```json
{
  "client": "hf-timestd",
  "version": "6.12.0",
  "instances": [
    {
      "instance": "default",
      "radiod_id": "k3lr-rx888",
      "host": "localhost",
      "required_cores": [],
      "preferred_cores": "worker",
      "frequencies_hz": [2500000, 3330000, 5000000],
      "ka9q_channels": 9,
      "disk_writes": [
        {"path": "/var/lib/timestd", "mb_per_day": 14000, "retention_days": 7}
      ],
      "uses_timing_calibration":     false,
      "provides_timing_calibration": true
    }
  ],
  "deps": {
    "git": [
      {"name": "ka9q-python", "commit": "abc1234"}
    ]
  }
}
```

**`<client> validate --json`** — self-validate every instance's config.
Shape:

```json
{
  "ok": true,
  "issues": [
    {"severity": "warn", "instance": "default", "message": "storage_quota above 90%"}
  ]
}
```

These are the only hooks sigmond relies on to learn about a client.
Sigmond shells them out as subprocesses — it never imports client code.

### 4. Systemd units

- Primary unit is **templated**: `<client>@.service`. The instance
  name is the systemd `%i`, which should match the `instance` field
  in the client's `inventory` output.
- Single-instance clients may additionally ship an
  unparameterized `<client>.service` that delegates to
  `<client>@default.service`.
- The unit **must** source the coordination environment with an
  optional EnvironmentFile directive (note the leading dash):
  ```ini
  [Service]
  EnvironmentFile=-/etc/sigmond/coordination.env
  EnvironmentFile=-/etc/<client-name>/env/%i.env
  ```
  The leading `-` makes the file optional so the unit remains
  runnable when sigmond is not installed.
- Sigmond writes CPU affinity only as drop-ins at
  `/etc/systemd/system/<client>@<instance>.service.d/10-sigmond-cpu-affinity.conf`.
  The client must not write its own drop-ins under that path.

### 5. Deploy manifest: `<repo>/deploy.toml`

Every client declares how its repo-tree code reaches its production
location. This replaces any hard-coded install knowledge sigmond
would otherwise carry.

```toml
[package]
name    = "hf-timestd"
version = "0.9.3"

[build]
# Optional. Omit for pure-Python clients.
steps = [
  "make -C src",
]
produces = ["src/timestd-core-recorder"]

[install]
# Sigmond executes these in order. All dst paths are absolute on
# the target; src paths are relative to the repo root.
# kind = "link" | "copy" | "render"

[[install.steps]]
kind = "link"
src  = "bin/timestd"
dst  = "/usr/local/sbin/timestd"

[[install.steps]]
kind = "copy"
src  = "src/timestd-core-recorder"
dst  = "/usr/local/sbin/timestd-core-recorder"
mode = "0755"

[[install.steps]]
kind = "link"
src  = "systemd/timestd-core-recorder@.service"
dst  = "/etc/systemd/system/timestd-core-recorder@.service"

[[install.steps]]
kind = "render"
src  = "config/timestd-config.toml.template"
dst  = "/etc/hf-timestd/instances/default.toml"
if_absent = true    # do not overwrite existing user config

[systemd]
# Sigmond uses this for start/stop/status enumeration.
units = ["timestd-core-recorder@.service"]

[deps]
[[deps.git]]
name       = "ka9q-python"
url        = "https://github.com/ka9q/ka9q-python.git"
commit     = "abc1234"
install_to = "/opt/ka9q-python"

[[deps.pypi]]
name    = "ka9q-python"
version = "3.3.0"
venv    = "/opt/hf-timestd/venv"
```

**Standalone-safe requirement:** the client must also ship an
equivalent `./install.sh` (or `make install`) that uses `deploy.toml`
as its source of truth. A user who installs without sigmond gets the
same production layout.

### 6. Talking to radiod: use `ka9q-python`

Any client that consumes RTP streams from a radiod instance does so
through the `ka9q-python` library (`RadiodControl` etc.). This
guarantees consistent channel reservation, status DNS resolution,
and teardown semantics across the whole suite — and it means
re-pointing a client at a different radiod is just a coordination.env
rewrite.

Clients are forbidden from speaking radiod's control protocol
directly.

## What sigmond promises in return

- Never writes inside a client's native config file.
- Reads `<client> inventory --json` to learn what the client wants.
- Publishes a single coordination.env file with authoritative
  per-radiod and station facts, updated atomically on each `smd
  apply`.
- Writes CPU affinity drop-ins in the client's own
  `<unit>.d/10-sigmond-cpu-affinity.conf` path and nowhere else.
- Never requires the client to depend on sigmond code or shell out to
  `smd`.

## Migration and versioning

- Contract version is declared in the top of this document and in
  sigmond's generic `contract.py` adapter. Sigmond will warn when a
  client's inventory output claims a newer version than the sigmond
  on the host supports.
- Existing clients (`hf-timestd`, `wsprdaemon-client`) are being
  retrofitted in Phase 2 of the sigmond plan. Expect 1-2 revisions
  to this doc as the retrofit surfaces real issues.
