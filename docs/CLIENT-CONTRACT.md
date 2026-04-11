# HamSCI Client Contract

**Version:** 0.2
**Status:** Adopted. First full v0.2 implementation is `hf-timestd`
v7.0.0 — see §9. v0.2 adds:

- **§7 — deterministic data multicast destination.**  A single station
  running multiple peer clients (hf-timestd, wsprdaemon, psk-recorder,
  ka9q-web, future clients) must not collide on radiod's default data
  multicast group.  The rule applies whether or not sigmond is
  coordinating the station.
- **§8 — radiod-scoped facts (BPSK PPS chain delay).**  Some corrections
  are properties of the radiod instance, not of any individual client,
  and must reach every client of that radiod.  First concrete case is
  WB6CXC BPSK PPS chain-delay calibration measured by hf-timestd and
  applied by every peer client.  Full implementation depends on sigmond
  Phase 4, but the client-side hook is in the contract now so new
  clients are born aware of it.

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
instance. Shape (example is the live v0.2 output from `hf-timestd` on
bee3, the reference implementation — see §9):

```json
{
  "client": "hf-timestd",
  "version": "7.0.0",
  "contract_version": "0.2",
  "config_path": "/etc/hf-timestd/timestd-config.toml",
  "git": {
    "sha": "96beda99f5c6b9e2ab452444825001c5d3320e95",
    "short": "96beda9",
    "ref": "main",
    "dirty": false
  },
  "instances": [
    {
      "instance": "default",
      "radiod_id": "bee3-rx888",
      "host": "localhost",
      "required_cores": [],
      "preferred_cores": "worker",
      "frequencies_hz": [2500000, 3330000, 5000000],
      "ka9q_channels": 9,
      "data_destination": "239.45.120.115",
      "radiod_status_dns": "bee3-status.local",
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
  },
  "issues": []
}
```

`contract_version` is the version of this document the client was built
against. Sigmond compares it to its own supported version and warns on
mismatch (see Migration and versioning, below). `git` is optional but
recommended — it lets `smd diag` answer "what's running?" without
shelling into the client.

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

**Stdout cleanliness — hard requirement.** Both subcommands must emit
**only** the JSON document to stdout. No banners, no "Logging
configured" lines, no progress dots. Any human-readable text — warnings,
info messages, banner — must go to stderr. Clients that initialize a
logger at import time need an explicit guard in `main()` (or equivalent)
that redirects the root logger to stderr before parsing args, so the
routine "Logging configured" line never lands in the JSON pipe. A
malformed first byte on stdout makes the whole inventory unparseable for
sigmond's `ContractAdapter`; this was the failure mode that prompted
adding the guard to `hf-timestd` in commit `339dec4`.

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

### 7. Deterministic data multicast destination (v0.2)

**Rule.** Every client that subscribes to radiod RTP data streams MUST
request its own data multicast destination when creating channels, and
MUST derive that destination deterministically from a per-client
identifier.  Clients MUST NOT rely on radiod's default data multicast
group for production.

**Why this is in the contract and not left to coordination.**  A single
station routinely runs several peer clients (hf-timestd, wsprdaemon,
psk-recorder, ka9q-web, future clients) without sigmond present.  If
each client leaves `destination` unset, every client lands on radiod's
configured default group (one multicast address, 5004/udp) and every
client's socket sees every other client's RTP packets.  The kernel
fans each packet out to every joined socket, every client's decoder
wakes up to SSRC-filter it, and jitter rises well before CPU does.
By making each client's destination a property of the client identity,
standalone installs are automatically non-overlapping and sigmond never
has to mediate address allocation.

**Derivation.**  Use `ka9q.generate_multicast_ip(unique_id)` with a
`unique_id` of shape `"<client-name>:<station_id>:<instrument_id>"`.
The helper is part of `ka9q-python`'s public API:

```python
from ka9q import generate_multicast_ip

client_id    = f"{CLIENT_NAME}:{station_id}:{instrument_id}"
destination  = generate_multicast_ip(client_id)  # e.g. '239.7.245.164'

channel_info = control.ensure_channel(
    frequency_hz=freq,
    preset="iq",
    sample_rate=24000,
    destination=destination,
    ...
)
```

`generate_multicast_ip()` hashes the id with SHA-256 and takes the
first three bytes as the last three octets of `239.x.y.z` — pure
function, zero-config, collision probability ≈ 1 / 16.7 M per pair.
Including `station_id` and `instrument_id` in the id ensures that the
same client type running on two hosts, or two instruments on the same
host, still lands on distinct groups.

**Override and resolution order.**  Clients MUST honor an explicit
override in their native config if present, so operators can resolve
collisions without a code change.  The reference implementation in
`hf-timestd` uses a three-step precedence that other clients SHOULD
mirror:

1. **Operator override** — `[ka9q] data_destination = "239.x.y.z"` in
   the client's native config. Used to resolve hand-diagnosed
   collisions. No other code path can override this.
2. **Legacy key** — a pre-v0.2 config key (for hf-timestd this is
   `[core] radiod_multicast_group`). Honored for rollback compatibility
   for one contract release, then removed. New clients SHOULD skip this
   step entirely.
3. **Derived default** — `generate_multicast_ip("<client>:<station>:<instrument>")`.
   This is what a blank install lands on.

The resolved value is what the client passes to every
`control.ensure_channel(destination=...)` call and what it reports in
`inventory --json`. Do not resolve lazily per-channel — resolve once at
startup and reuse, so `inventory` and runtime never diverge.

**Sigmond side.**  Sigmond MUST NOT pre-allocate or override data
multicast addresses on a client's behalf.  Sigmond MAY read each
client's chosen destination from the `inventory --json` output (see
below) and use it for diagnostics, routing, or collision detection.
If sigmond detects two clients claiming the same address, that is a
hard error surfaced through `smd diag` — sigmond does not silently
reassign.

**Inventory surface.**  Every instance entry in `<client> inventory --json`
MUST include a `data_destination` field — the multicast IP the instance
is currently configured to use — so that sigmond and operators can see
the binding without running `ss`/`ipcs`.  For clients that expose
multiple streams on distinct groups (rare), this MAY be an array; the
common case is a scalar string.

```json
{
  "instances": [
    {
      "instance": "default",
      "radiod_id": "bee3-rx888",
      "data_destination": "239.7.245.164",
      "ka9q_channels": 9,
      ...
    }
  ]
}
```

**Standalone requirement — the hard rule.**  Running two peer clients
on the same host with no sigmond present MUST NOT result in multicast
collisions.  A new client type is contract-conformant only if a blank
install with default configs on the same host as an existing conformant
client produces two distinct multicast groups with no operator action.

### 8. Radiod-scoped facts: BPSK PPS chain delay (v0.2)

Some per-radiod facts do not belong to any one client but MUST reach
every client that subscribes to that radiod.  The first of these is
the BPSK PPS chain-delay correction measured by an hf-timestd instance
running WB6CXC's injector hardware: it tells every consumer of that
radiod's RTP streams how many nanoseconds to subtract from RTP-derived
UTC to get a true GPS-disciplined timestamp.  That correction is a
property of the *analog front-end + ADC + radiod pipeline*, not of
any individual client, so every client of that radiod — wsprdaemon,
psk-recorder, ka9q-web, a second hf-timestd instance in a different
timing role — has to apply the same number.

**Distribution.**  When sigmond is present, the authoritative source
is `/etc/sigmond/coordination.env`:

```
RADIOD_BEE3_RX888_CHAIN_DELAY_NS=4250
RADIOD_BEE3_RX888_CHAIN_DELAY_SOURCE=hf-timestd@bee3
RADIOD_BEE3_RX888_CHAIN_DELAY_UPDATED=2026-04-11T07:24:31Z
```

The key format is `RADIOD_<id>_CHAIN_DELAY_NS`, matching sigmond's
existing convention for per-radiod facts (see `RADIOD_<id>_STATUS`,
`RADIOD_<id>_SAMPRATE`).  When the calibrating hf-timestd instance
locks a new chain-delay value, it calls into sigmond via a hook
(`smd radiod-fact set <id> chain_delay_ns <value>`, or equivalent
write to coordination.env — mechanism TBD in sigmond Phase 4); sigmond
rewrites coordination.env atomically and sends SIGHUP (or systemd
reload) to every service whose unit file has
`EnvironmentFile=-/etc/sigmond/coordination.env`.

**Client-side requirement.**  Every client that reads RTP data from a
radiod MUST, on startup and on reload, check for
`RADIOD_<id>_CHAIN_DELAY_NS` in the environment and apply it to every
sample-to-UTC conversion derived from that radiod.  Clients that do
not do timing-critical work (e.g. ka9q-web's waterfall) MAY ignore it
but MUST NOT propagate the raw RTP-derived UTC to downstream consumers
without correction.  The application is a simple subtraction:

```python
utc_corrected = utc_raw - chain_delay_ns / 1e9
```

and this happens once, at the boundary between radiod-observed samples
and whatever the client treats as "now".  Clients MUST surface the
value they are currently applying in their `inventory --json` output
as a new field:

```json
{
  "instance": "default",
  "radiod_id": "bee3-rx888",
  "chain_delay_ns_applied": 4250,
  ...
}
```

A `null` or missing value means "no correction is being applied"
(either sigmond has not published one, or the client is running
standalone without the hook).  Sigmond's `smd diag` surfaces any
mismatch across peer clients of the same radiod.

**Standalone behaviour.**  Without sigmond, each client reads
`chain_delay_ns` from its own config as a fallback:

```toml
[ka9q]
radiod_id        = "bee3-rx888"
radiod_status    = "bee3-status.local"
chain_delay_ns   = 4250    # optional, overridden by coordination.env when sigmond is present
```

This preserves the standalone-safe property: an operator without
sigmond can still apply a manually measured chain delay by setting it
in their client config.

**Why it belongs in the contract.**  Without this rule, the only
client that knows about chain delay is the one running the calibrator
— and that's the only client whose timestamps are correct.  Every
other peer of that radiod silently reports timing that is offset by
the chain delay.  Making the distribution part of the contract is
the mechanism that lets hf-timestd measure the correction, publish
it via sigmond, and have psk-recorder + wsprdaemon + any other client
pick it up automatically.  The calibration is hf-timestd's
responsibility; the *distribution* is sigmond's; the *application* is
every peer client's.

**Dependency note.**  §8 depends on sigmond Phase 4 (sigmond takes
over cross-client write paths into coordination.env).  Until Phase 4
lands, hf-timestd will apply the correction to its own channels only
(current behaviour in `core_recorder_v2._l6_on_samples`), the field
will be published to `inventory --json` unconditionally, and sigmond
will warn but not fail if it sees a non-null `chain_delay_ns_applied`
with no matching `RADIOD_*_CHAIN_DELAY_NS` in coordination.env.

### 9. Reference implementation: hf-timestd v7.0.0

`hf-timestd` at tag [`v7.0.0`](https://github.com/mijahauan/hf-timestd/releases/tag/v7.0.0)
is the first full v0.2-conformant client. When in doubt about the
shape of a subcommand, the wording of an error, or the precedence
order for data-destination resolution, read hf-timestd's code — the
contract follows what hf-timestd ships, not the other way round.

Concrete pointers:

- **`inventory` / `validate` subcommands** —
  [cli.py](https://github.com/mijahauan/hf-timestd/blob/v7.0.0/src/hf_timestd/cli.py),
  commit [`339dec4`](https://github.com/mijahauan/hf-timestd/commit/339dec4).
  Note the stdout-cleanliness guard at the top of `main()`.
- **§7 data-destination resolution** —
  [`core_recorder_v2.__init__`](https://github.com/mijahauan/hf-timestd/blob/v7.0.0/src/hf_timestd/core/core_recorder_v2.py),
  commit [`2b83793`](https://github.com/mijahauan/hf-timestd/commit/2b83793).
  Implements the 3-step precedence above. The resolved address is
  passed to every `StreamRecorderConfig(destination=...)` and to the
  L6 BPSK PPS `ensure_channel()` call.
- **`deploy.toml`** — at repo root; real worked example of `[build]`,
  `[install.steps]`, `[systemd]`, and `[deps]`.
- **§8 chain-delay hook** — hf-timestd is the *calibrator* for chain
  delay, so its current code applies the correction only to its own
  channels; the `RADIOD_<id>_CHAIN_DELAY_NS` *publish* side is the
  sigmond Phase 4 work. A peer client (e.g. psk-recorder) reading the
  env var is the simpler side and can be added today against any v0.2
  client by imitating what's in §8's code snippet.

If a retrofit or greenfield build uncovers a gap between the contract
as written here and what hf-timestd ships, fix the contract — update
this document and bump the version — rather than adding a
per-client special case to sigmond's `ContractAdapter`.

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
- **v0.1 → v0.2** added §7 (deterministic data multicast destination)
  and the `data_destination` field in `inventory --json`.  v0.1 clients
  still pass validation on a v0.2 sigmond; sigmond treats a missing
  `data_destination` as a contract warning, not an error, for one
  release.
- Existing clients (`hf-timestd`, `wsprdaemon-client`) are being
  retrofitted alongside the contract bump.  `hf-timestd` lands §7
  compliance in the same cycle that retires its legacy v1 recorder
  stack (`channel_recorder.py`), which was the source of the now-dead
  `generate_timestd_multicast_ip` helper that partially implemented
  this rule but was never reachable from the production V2 path.
