# HamSCI Client Contract

**Version:** 0.4
**Status:** Adopted. First full v0.2 implementation is `hf-timestd`
v7.0.0 — see §9.  First greenfield v0.3 implementation is
`psk-recorder` v0.1.0, which also surfaced the v0.4 hardening items in
§12.  v0.4 adds:

- **§12 (new) — validate hardening and deploy safety.**  Six concrete
  checks surfaced by the psk-recorder Phase 1 deploy on 2026-04-13.
  Three are MUST (entry-point reachability, SSRC uniqueness, deployed
  config path disclosure); three are SHOULD (decoder-mutation of
  spool, Pattern A canonical layout, ka9q-python PyPI-lag check).

v0.3 adds:

- **§7 revised — data multicast is now a `ka9q-python` concern.**
  Clients MUST NOT pass `destination=` to `ensure_channel()`.
  `ka9q-python` derives the multicast group deterministically and
  returns the resolved address in `ChannelInfo`.  Clients read it for
  `inventory --json` but never select it.  This simplifies every
  client and eliminates the `generate_multicast_ip()` call-site
  pattern from v0.2.  Operator overrides belong in radiod config, not
  in client config.  The standalone collision-avoidance property is
  preserved because `ka9q-python` still uses per-client-identity
  derivation internally.
- **§10 (new) — logging discipline and discovery.**  Clients MUST log
  to stderr (systemd journal).  File logs, if any, live under
  `/var/log/<client>/` and are surfaced in `inventory --json` via a
  new `log_paths` object.
- **§11 (new) — runtime log level controlled by sigmond.**  Sigmond
  publishes `<CLIENT>_LOG_LEVEL` in `coordination.env`; clients honor
  it on startup and on SIGHUP.  Enables `smd log --level=DEBUG <client>`
  without config edits or restarts.

Previous v0.2 additions (unchanged):

- **§7 (original motivation) — deterministic data multicast destination.**
  A single station running multiple peer clients must not collide on
  radiod's default data multicast group.  The rule applies whether or
  not sigmond is coordinating the station.  (v0.3 moves the derivation
  from client code into `ka9q-python`; the requirement is preserved.)
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

### 7. Deterministic data multicast destination (v0.2, revised v0.3)

**Rule.** Every client that subscribes to radiod RTP data streams MUST
use `ka9q-python`'s `RadiodControl.ensure_channel()` for channel
creation.  Clients MUST NOT pass a `destination=` argument to
`ensure_channel()`.  `ka9q-python` derives the multicast destination
deterministically and returns the resolved address in `ChannelInfo`.
Clients read this value for `inventory --json` reporting but never
select or compute it.

**Why this changed from v0.2.**  v0.2 required clients to call
`generate_multicast_ip()` themselves and pass `destination=` on every
`ensure_channel()` call.  This duplicated logic across every client,
created a maintenance surface for the derivation formula, and required
each client to carry station-id / instrument-id fields solely for
multicast derivation.  Moving the derivation into `ka9q-python` means:
(a) every client automatically gets the collision-avoidance property
with zero per-client code, (b) the derivation formula can evolve in
one place, and (c) clients without PSWS station identifiers (e.g.
`psk-recorder`) work correctly without any special handling.

**Motivation (unchanged from v0.2).**  A single station routinely runs
several peer clients (hf-timestd, wsprdaemon, psk-recorder, ka9q-web,
future clients) without sigmond present.  If every client lands on
radiod's default data multicast group, the kernel fans each RTP packet
to every joined socket, jitter rises, and decoding quality degrades.
`ka9q-python` assigns per-client destinations that are automatically
non-overlapping.  Sigmond never has to mediate address allocation.

**Client code.**  Channel creation is a simple `ensure_channel()` call
with no destination argument:

```python
channel_info = control.ensure_channel(
    frequency_hz=freq,
    preset="usb",
    sample_rate=12000,
)
# ka9q-python allocated the destination; read it for inventory:
resolved_destination = channel_info.destination
```

**Operator override.**  If an operator needs to force a specific
multicast group (e.g. to resolve a rare collision), the override goes
in radiod's config or in a `ka9q-python`-level configuration, NOT in
the client's config.  Client configs SHOULD NOT contain a
`data_destination` field.  Clients that previously implemented the
v0.2 three-step override precedence (operator override → legacy key →
derived default) SHOULD remove it when upgrading to v0.3 and delete
any `data_destination` / `radiod_multicast_group` config keys.

**Sigmond side.**  Sigmond MUST NOT pre-allocate or override data
multicast addresses on a client's behalf.  Sigmond MAY read each
client's resolved destination from the `inventory --json` output and
use it for diagnostics, routing, or collision detection.  If sigmond
detects two clients claiming the same address, that is a hard error
surfaced through `smd diag` — sigmond does not silently reassign.

**Inventory surface.**  Every instance entry in `<client> inventory --json`
MUST include a `data_destination` field — the multicast IP the instance
is currently using, as reported by `ka9q-python` — so that sigmond and
operators can see the binding without running `ss`/`ipcs`.

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
This property is now guaranteed by `ka9q-python` rather than by
per-client derivation code.

**Migration from v0.2.**  `hf-timestd` v7.0.0 implements the v0.2
pattern (client-side `generate_multicast_ip()` + `destination=`).  A
follow-up release should remove that code and rely on `ka9q-python`
once the library ships the internal derivation.  Until then, hf-timestd
remains conformant — it just does more work than necessary.

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

### 10. Logging discipline and discovery (v0.3)

Sigmond needs to locate a client's logs for diagnostics (`smd log`,
`smd diag`) without guessing directory layouts.  This section
standardizes where clients log and how they tell sigmond about it.

**Primary log channel.**  Clients MUST log normal operation to stderr.
When running under systemd, stderr is captured by the journal.
`smd log <client>` is then a thin wrapper around
`journalctl -u <unit>`.

**File logs (optional).**  Clients MAY additionally write persistent
file logs (spot logs, decode output, structured event logs).  If they
do:

1. All file logs MUST live under `/var/log/<client-name>/`.  Clients
   MUST NOT write logs anywhere else on the filesystem.
2. The systemd unit MAY use `StandardOutput=append:/var/log/<client>/...`
   for the process log, but this duplicates the journal and is not
   required.
3. Clients MUST surface every file-log path in `inventory --json`
   under a top-level `log_paths` object:

```json
{
  "client": "psk-recorder",
  "log_paths": {
    "process": "/var/log/psk-recorder/bee1-rx888.log",
    "spots": {
      "ft8": "/var/log/psk-recorder/bee1-rx888-ft8.log",
      "ft4": "/var/log/psk-recorder/bee1-rx888-ft4.log"
    }
  },
  ...
}
```

The keys inside `log_paths` are client-defined; sigmond treats the
object as opaque and presents all paths to the operator via
`smd log <client> --files`.  If a client writes no file logs,
`log_paths` SHOULD be omitted (not an empty object).

**Sigmond side.**  `smd log <client>` defaults to
`journalctl -u <unit> --follow`.  With `--files`, it reads
`log_paths` from the client's inventory and tails the named files.

### 11. Runtime log level controlled by sigmond (v0.3)

Sigmond can adjust a client's verbosity at runtime without editing the
client's config file or restarting the service.

**Environment variable.**  Sigmond MAY publish a per-client log level
in `coordination.env`:

```
PSK_RECORDER_LOG_LEVEL=DEBUG
```

The variable name is `<CLIENT_NAME>_LOG_LEVEL` where `<CLIENT_NAME>`
is the client's name in SCREAMING_SNAKE_CASE with hyphens replaced by
underscores (e.g. `hf-timestd` → `HF_TIMESTD_LOG_LEVEL`,
`psk-recorder` → `PSK_RECORDER_LOG_LEVEL`).

Sigmond MAY also publish a generic fallback:

```
CLIENT_LOG_LEVEL=WARNING
```

**Values.**  Standard Python `logging` level names: `DEBUG`, `INFO`,
`WARNING`, `ERROR`, `CRITICAL`.  Case-insensitive.

**Client-side requirement.**  Every client MUST resolve its log level
on startup using this precedence (highest-priority first):

1. **Command-line flag** — `--log-level <level>` for one-shot debug
   runs.
2. **Client-specific env var** — `<CLIENT>_LOG_LEVEL`.
3. **Generic env var** — `CLIENT_LOG_LEVEL`.
4. **Client config** — e.g. `[logging] level = "INFO"` in the client's
   native config.
5. **Default** — `INFO`.

Clients running as long-lived daemons MUST install a `SIGHUP` handler
that re-reads the environment variables (steps 2 and 3) and re-applies
the resolved level to the root logger without restarting RTP streams
or other active work.  This makes `smd log --level=DEBUG <client>` a
one-step operation: sigmond rewrites `coordination.env` and sends
`SIGHUP` to the unit.

**Inventory surface.**  Clients SHOULD report their current effective
log level in `inventory --json` as a top-level field:

```json
{
  "client": "psk-recorder",
  "log_level": "INFO",
  ...
}
```

This lets `smd diag` show at a glance which clients are running in
debug mode without parsing logs.

**Interaction with §3 stdout cleanliness.**  The log-level mechanism
applies to the daemon and status subcommands.  The `inventory` and
`validate` subcommands MUST still suppress all logging to stdout
regardless of the configured level — the §3 stdout-cleanliness guard
takes priority.

### 9. Reference implementations

**v0.2 reference: hf-timestd v7.0.0.**
[`hf-timestd` v7.0.0](https://github.com/mijahauan/hf-timestd/releases/tag/v7.0.0)
is the first full v0.2-conformant client. It remains the reference for
§1–§6, §8, and the stdout-cleanliness guard in §3.

Concrete pointers:

- **`inventory` / `validate` subcommands** —
  [cli.py](https://github.com/mijahauan/hf-timestd/blob/v7.0.0/src/hf_timestd/cli.py),
  commit [`339dec4`](https://github.com/mijahauan/hf-timestd/commit/339dec4).
  Note the stdout-cleanliness guard at the top of `main()`.
- **`deploy.toml`** — at repo root; real worked example of `[build]`,
  `[install.steps]`, `[systemd]`, and `[deps]`.
- **§8 chain-delay hook** — hf-timestd is the *calibrator* for chain
  delay, so its current code applies the correction only to its own
  channels; the `RADIOD_<id>_CHAIN_DELAY_NS` *publish* side is the
  sigmond Phase 4 work.

**v0.3 retrofit needed for hf-timestd:**
- §7: remove client-side `generate_multicast_ip()` and
  `destination=` from `ensure_channel()` calls; delete the
  `data_destination` override / `radiod_multicast_group` legacy key
  from config. Read `data_destination` from `ChannelInfo` instead.
- §10: add `log_paths` to `inventory --json` output, pointing at
  `/var/log/hf-timestd/`.
- §11: honor `HF_TIMESTD_LOG_LEVEL` and `CLIENT_LOG_LEVEL` env vars
  on startup and SIGHUP. Install a SIGHUP handler in the recorder's
  main loop.
- Bump `contract_version` in inventory output from `"0.2"` to `"0.3"`.

**v0.3 greenfield reference: psk-recorder v0.1.0.**
[`psk-recorder`](https://github.com/mijahauan/psk-recorder) is the
first client built against v0.3 from day one.  It implements §7
(no `destination=`), §10 (log paths in inventory), and §11 (runtime
log level) natively.

If a retrofit or greenfield build uncovers a gap between the contract
as written here and what the reference clients ship, fix the contract
— update this document and bump the version — rather than adding a
per-client special case to sigmond's `ContractAdapter`.

### 12. Validate hardening and deploy safety (v0.4)

These six items were surfaced during the `psk-recorder` Phase 1 deploy
on 2026-04-13.  Each is labeled **MUST** (a hard `validate` check,
exit nonzero on violation) or **SHOULD** (a warning in `validate`
output and/or an operator-docs requirement).

#### 12.1 Entry-point reachability (MUST)

The command declared in `deploy.toml`'s `[systemd].exec_start` (or the
equivalent default, typically `python -m <module>`) MUST actually
reach the daemon's `main()`.  `validate` MUST assert the invocation
would dispatch past the module-load phase — a missing
`if __name__ == "__main__": main()` guard in the CLI module is a
contract violation, not a user bug.

*Rationale:* `psk-recorder` shipped with `python -m psk_recorder.cli`
as the unit's `ExecStart`, but `cli.py` had no `__main__` guard.  The
module loaded and returned, systemd saw a clean exit 0 in ~100 ms
with no log output, and `Type=notify` reported failure with no
diagnostic.  See
[psk-recorder 520e39f](https://github.com/mijahauan/psk-recorder/commit/520e39f).

*Acceptance:* a reference check is "import the CLI module under a
sentinel `__name__` and assert `main()` is registered as the entry";
a simpler sufficient check is a grep-style assertion on the CLI
source.  Either satisfies the MUST.

#### 12.2 SSRC uniqueness across a radiod block (MUST)

`ka9q.addressing.compute_ssrc(freq, preset, sample_rate, encoding)`
is a pure function of its arguments.  Two channels with identical
`(freq, preset, sample_rate, encoding)` tuples collide on SSRC, and
`ka9q-python`'s `MultiStream` keys its slot dict by SSRC — the second
`add_channel()` silently overwrites the first's callback, dropping
one sink without any error.

`validate` MUST reject a config that produces duplicate SSRC tuples
within a single `[[radiod]]` block, naming both offending entries.
This turns a silent runtime drop into a config-time error.

*Rationale:* `psk-recorder` initially shipped with FT4 1.840 MHz and
FT8 1.840 MHz both configured (same preset `usb`, same 12 kHz rate,
same `s16be` encoding).  The running daemon reported "20 channels
provisioned" but only 19 active; the FT4 160 m sink was silently
dead.  FT4 has no standard 160 m calling frequency, so the entry was
bogus — but a uniqueness check would have caught it at first
`validate` run.  See
[psk-recorder be4a050](https://github.com/mijahauan/psk-recorder/commit/be4a050).

#### 12.3 Deployed config path disclosure (MUST)

`validate --json` and `inventory --json` MUST include the **absolute
path of the config file actually loaded**, as a top-level field
`config_path`.  Clients with a precedence chain (env override →
`/etc/<client>/…` → repo template) MUST report the chosen path, not
the first candidate.

*Rationale:* the repo's `config/<client>-config.toml` and the
installed `/etc/<client>/<client>-config.toml` drift the moment
`install.sh` copies the template on first install.  During the
psk-recorder Phase 1 deploy, an edit to the repo template was
committed, pushed, and restarted — with no effect on the running
daemon, because it was reading `/etc/psk-recorder/...`.  Making the
loaded path visible in inventory/validate output eliminates the class
of "I edited the config and nothing changed" bugs for both operators
and agents.

*Acceptance:* `inventory --json | jq -r .config_path` prints a single
absolute path; that path is the one the daemon would load if started
now.

#### 12.4 Decoder subprocesses may mutate the spool (SHOULD)

External decoder processes (e.g. `decode_ft8`) are permitted to
delete, rename, or rewrite files in the spool directory.  Clients
that require file retention (debugging snapshots, `keep_wav=true`,
post-hoc analysis) MUST snapshot the file **before** forking the
decoder, into a directory the client controls exclusively.  Under
`ProtectSystem=strict` systemd units, the snapshot destination must
be listed in `ReadWritePaths`; `/tmp` is not writable under that
hardening.

*Rationale:* `decode_ft8` unconditionally unlinks the WAV it just
decoded.  psk-recorder's `keep_wav=true` code path does the right
thing locally (it skips its own unlink) but cannot retain the file
because the decoder subprocess deletes it first.  The contract
shouldn't dictate the decoder's behavior, but clients must be aware
that shared-spool lifecycle is not under their control.

Clients SHOULD document this in their config reference next to any
retention flag.

#### 12.5 Pattern A canonical repo layout (SHOULD)

The canonical repo location for a sigmond-managed HamSCI client is
**`/opt/git/sigmond/<client>`**, owned `mjh:<service-group>` and
group-writable, with a convenience symlink `~/git/<client> →
/opt/git/sigmond/<client>`.  The service user must be a member of
`<service-group>`.  The `/opt/git/sigmond/` namespace is reserved for
clients sigmond installs and discovers; non-sigmond infra repos
(`ka9q-radio`, `ka9q-web`, `ka9q-python`, `ka9q-update`) live in the
parent `/opt/git/` directly so they remain available for general use.

Anti-pattern: `install.sh` writing a symlink `/opt/git/sigmond/<client>
→ ~/git/<client>`.  This fails the mode-700 home-traversability check
for service users and must not be shipped in new client install
scripts.  `hf-timestd` and `psk-recorder` both originally hit this
trap; both now use Pattern A.

Sigmond deployment docs and new-client install scripts SHOULD codify
Pattern A as the default and ban the reverse symlink.

#### 12.6 ka9q-python PyPI lag (SHOULD)

Clients that pin `ka9q-python>=X.Y.Z` in `pyproject.toml` depend on
PyPI having that version published.  `validate` SHOULD check the
installed `ka9q-python.__version__` against the client's declared
minimum and emit a warning — not just an import error — if the
installed version is older than the minimum.  An installed version
*equal to* the minimum passes; a version older than what PyPI
currently offers when the client's minimum was bumped is a red flag
that `pip install -U` has not run or that the wheel was not yet
published at install time.

*Rationale:* psk-recorder's Phase 1 unit file required `MultiStream`,
which landed in `ka9q-python` 3.8.0; PyPI only had 3.7.1 at deploy
time.  The fix was a same-day PyPI publish.  A version check in
`validate` would have surfaced this before the service restart.

## What sigmond promises in return

- Never writes inside a client's native config file.
- Reads `<client> inventory --json` to learn what the client wants.
- Publishes a single coordination.env file with authoritative
  per-radiod and station facts, updated atomically on each `smd
  apply`.
- Writes CPU affinity drop-ins in the client's own
  `<unit>.d/10-sigmond-cpu-affinity.conf` path and nowhere else.
- MAY publish per-client log levels in coordination.env and send
  SIGHUP to the client's unit after changes (§11).
- MAY read `log_paths` from inventory to locate client file logs
  (§10).
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
- **v0.2 → v0.3** revises §7 (data multicast derivation moves into
  `ka9q-python`; clients drop `destination=` and `generate_multicast_ip()`
  call sites), adds §10 (logging discipline + `log_paths` in inventory),
  and §11 (runtime log level via `<CLIENT>_LOG_LEVEL` env var + SIGHUP).
  v0.2 clients still pass validation on a v0.3 sigmond; sigmond treats
  a missing `log_paths` or `log_level` as informational, not an error.
  Clients that still pass `destination=` to `ensure_channel()` remain
  operationally correct — `ka9q-python` honors an explicit destination —
  but should remove the call-site when upgrading.
- **Clients requiring v0.3 retrofit:** `hf-timestd` (§7 simplification,
  §10, §11 — see §9 retrofit checklist above).  `wsprdaemon-client`
  needs the full v0.2 + v0.3 retrofit (contract subcommands, deploy.toml,
  logging).
- **v0.3 → v0.4** adds §12 (validate hardening and deploy safety).
  Three MUST items: §12.1 entry-point reachability, §12.2 SSRC
  uniqueness, §12.3 `config_path` in inventory/validate output.
  Three SHOULD items: §12.4 decoder-mutation awareness, §12.5
  Pattern A canonical layout, §12.6 ka9q-python PyPI-lag check.
  v0.3 clients still pass validation on a v0.4 sigmond; sigmond
  treats a missing `config_path` or absent SSRC-uniqueness check as
  a contract warning, not an error, for one release.
- **Clients requiring v0.4 retrofit:** `psk-recorder` v0.1.1 (add
  §12.2 uniqueness check to `validate`, §12.3 `config_path` field —
  the Phase 1 deploy proved both items live), `hf-timestd` (same two
  items, bundled with the v0.3 retrofit work).  `wsprdaemon-client`
  targets v0.4 directly.
- `psk-recorder` was built greenfield against v0.3; v0.4 additions
  are a minor-release retrofit, not a fresh rewrite.
