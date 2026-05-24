# HamSCI Client Contract

**Version:** 0.7
**Status:** Adopted. First full v0.2 implementation is `hf-timestd`
v7.0.0 — see §9.  First greenfield v0.3 implementation is
`psk-recorder` v0.1.0, which also surfaced the v0.4 hardening items in
§12.  The conformant clients (hf-timestd, psk-recorder,
wspr-recorder) retrofitted to v0.5 on 2026-05-04.
§17 (output sinks) drafted 2026-05-07 and revised to keep the sink
`kind` taxonomy engine-agnostic.
§18 (timing authority and the RTP-default fallback) added 2026-05-24,
giving the latent v0.2 booleans (`uses_timing_calibration`,
`provides_timing_calibration`) their contract semantics for the first
time.

v0.7 adds:

- **§18 (new) — timing authority and the default fallback.**
  Defines two operating modes (default and authority-corrected),
  two subscriber substrates (radiod RTP and non-radiod), the
  discovery mechanism (per-radiod keys via `coordination.env`
  parallel to §8, plus station-wide keys for non-radiod clients
  like `mag-recorder` and KiwiSDR-based recorders), the snapshot
  fields a subscribing client may rely on (`utc_anchor_ns`,
  `tier`, `sigma_ns`, `snapshot_age_s` for all subscribers;
  `rtp_anchor_sample`, `rate_samples_per_utc_sec`, `radiod_id` for
  radiod subscribers; `host_monotonic_at_anchor` for non-radiod
  subscribers), client obligations for sample labelling vs.
  hard-deadline start/stop, and the §18/§8 composition rule
  (§8 chain-delay is radiod-specific and does not apply to
  non-radiod clients).  The producer-side reference is
  `hf-timestd/docs/ARCHITECTURE-FIRST-PRINCIPLES.md`; the contract
  names what clients may rely on without specifying the wire
  protocol.  Default mode (RTP-default for radiod clients,
  host-clock-default for non-radiod clients) remains conformant;
  no client is required to be hf-timestd-aware.
- **§3 amendment.**  Adds `timing_authority_applied` per instance
  (null = §18 RTP-default mode, populated = authority-corrected
  with source/tier/σ/age).  Defines the v0.2 booleans
  `uses_timing_calibration` and `provides_timing_calibration` that
  previously appeared in inventory without contract semantics.
- **§8 amendment.**  Clarifying sentence: §8 is the *static*
  hardware-pipeline correction; §18 is the *dynamic* timeline-anchor
  correction; they compose, do not replace each other.

v0.6 adds:

- **§17 (new) — output sinks.**  Symmetric counterpart to §16's
  `data_path` (input).  New `data_sinks` array per instance in
  inventory: each entry declares `kind ∈ {file, service}`,
  `target`, `schema_ref`, `retention_days`, `mb_per_day`, and (for
  `service` sinks) a `health` field.  Backwards compat is
  unconditional: a v0.5 client that publishes only `disk_writes`
  is auto-promoted into the equivalent file-sink form.  The `kind`
  enum is deliberately engine-agnostic: the contract-relevant
  distinction is a local file sigmond disk-budgets versus an
  external service sigmond health-checks, not a database product.

v0.5 adds:

- **§5 (clarified) — lifecycle scope and unit declaration.**  New
  subsections 5.0–5.5 nail down `units` vs `templated_units` in
  `deploy.toml`, instance enumeration from
  `/etc/<client>/env/<instance>.env`, lifecycle scope boundaries,
  the `reload` verb's `ExecReload` convention, start/stop ordering,
  and the lifecycle lock at `/var/lib/sigmond/lifecycle.lock`.
- **§13 (new) — control surface.**  Each running client exposes a
  unix-socket HTTP/JSON endpoint with mandatory `/healthz`, `/readyz`,
  `/status`, `/metrics`, plus optional `/channels`, `/events`,
  `/reload`.  `/status` schema is the basis for inter-client
  diagnostics in `smd status` / `smd diag` (multicast collisions,
  IGMP-snooping silent failure, shared-spool exhaustion, CPU budget
  breach, radiod loss, back-pressure cascade).
- **§14 (new) — configuration interview.**  `[contract.config]` block
  in `deploy.toml` advertises the `init` and `edit` entry points
  sigmond invokes for guided configuration.  Sigmond passes a stable
  env var bag (`STATION_CALL`, `STATION_GRID`, `SIGMOND_RADIOD_*`,
  `SIGMOND_TIME_SOURCE`, etc.) so the operator doesn't retype the
  same callsign across five wizards.  Sigmond owns radiod's own
  config flow: `smd config init radiod` probes USB, prompts per SDR,
  renders `radiod@<id>.conf`, and registers in `coordination.toml`.
- **§15 (new) — radiod channel contributions.**  Clients declare
  `[[radiod.fragment]]` blocks in `deploy.toml` so sigmond installs
  rendered fragments to `/etc/radio/radiod@<id>.conf.d/` instead of
  every `install.sh` writing its own.
- **§16 (new) — independent data-source clients.**  `data_path.kind`
  per instance in inventory: `radiod-ka9q-python` (Path A, default),
  `radiod-direct` (Path B, explicit obligations), `kiwisdr`, `file`,
  `other`.  §16.3.1 covers the meta-client pattern (e.g. a decoder
  that reads another recorder's WAV spool) where `kind = "file"`
  with `details.upstream_client` names a sibling that supplies the
  WAVs.
- **§3 amendment.**  Inventory adds `control_socket` (per §13.1) and
  `deploy_toml_path` (per §5) per instance.
- **§6 amendment.**  The MUST on `ka9q-python` is relaxed to SHOULD,
  with §16's runtime invariants preserved as MUST.  Purely additive:
  v0.4 clients using `ka9q-python` remain conformant unchanged;
  sigmond infers `data_path.kind = "radiod-ka9q-python"` when the
  field is absent.

v0.4 adds:

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
      "provides_timing_calibration": true,
      "data_path":      {"kind": "radiod-ka9q-python", "radiod_id": "bee3-rx888"},
      "control_socket": "/run/hf-timestd/control.sock",
      "deploy_toml_path": "/opt/git/sigmond/hf-timestd/deploy.toml"
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

**Per-instance v0.5 fields:**

- **`data_path`** (v0.5) — names how the instance receives samples.
  See §16.3 for the full enum (`radiod-ka9q-python`, `radiod-direct`,
  `kiwisdr`, `file`, `other`).  Sigmond infers
  `kind = "radiod-ka9q-python"` when the field is absent, preserving
  v0.4 conformance.
- **`control_socket`** (v0.5) — path to this instance's
  control-socket endpoint (§13.1).  Sigmond uses this for discovery
  so the socket-path convention is a default, not a hardcoded
  assumption.  If absent, sigmond falls back to the §13.1 path.
- **`deploy_toml_path`** (v0.5) — path to the client's `deploy.toml`.
  Sigmond discovers each client's lifecycle declarations (§5) here
  rather than inventing a second discovery mechanism.  If absent,
  sigmond falls back to `/opt/git/sigmond/<client-name>/deploy.toml`
  (Pattern A canonical location, §12.5).

**Per-instance v0.6 fields:**

- **`data_sinks`** (v0.6) — array of output sinks per instance,
  symmetric to §16's `data_path` (input).  See §17 for the entry
  shape and the `disk_writes` auto-promotion path that keeps v0.5
  clients conformant unchanged.

**Per-instance v0.7 fields:**

- **`timing_authority_applied`** (v0.7) — `null` or missing if the
  instance operates in §18 RTP-default mode (the safe default).
  Otherwise, an object naming the authority source and the snapshot
  currently in use:

  ```json
  {
    "timing_authority_applied": {
      "source": "hf-timestd@bee3",
      "tier": "T5",
      "sigma_ns": 1200,
      "snapshot_age_s": 4.2,
      "radiod_id": "bee3-rx888"
    }
  }
  ```

  See §18.5 for the obligations behind this field.

**Per-instance v0.2 booleans (semantics defined in v0.7):**

These two fields have appeared in `inventory --json` since v0.2
without the contract ever defining their meaning.  §18 (v0.7)
gives them their semantics:

- **`uses_timing_calibration`** — `true` if the instance ever
  subscribes to a §18 timing authority (i.e. ever operates in
  authority-corrected mode), regardless of the mode currently
  active.  `false` for clients that always operate in §18
  RTP-default mode and never consult an authority.  The current
  mode is reported by `timing_authority_applied`.
- **`provides_timing_calibration`** — `true` if the instance is
  itself a §18 timing authority that other clients may subscribe
  to.  Currently only `hf-timestd`; reserved for future producers.

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

#### 5.0 Declaring units in `deploy.toml` (v0.5)

The `[systemd]` block above declared a single `units` array.  v0.5
splits unit declarations into two kinds so multi-instance clients can
be modeled declaratively:

```toml
[systemd]
units           = ["foo.service", "foo-daily.timer", "foo.target"]      # concrete names
templated_units = ["foo@.service", "foo-index@.timer"]                  # templates
```

- **`units`** — concrete unit names (services, timers, targets).
  Sigmond starts/stops these by their literal names.
- **`templated_units`** — template names (containing `@`) that
  sigmond will instantiate per discovered instance (see §5.1).
- Either key MAY be absent (treated as empty).

**Backward compatibility.** A templated name (contains `@.service` /
`@.timer` / `@.target`) appearing in `units` is deprecated but
accepted for v0.4 clients already deployed on production (e.g.
`psk-recorder@.service` in v0.4 releases).  Sigmond detects the `@`
marker, normalizes it into `templated_units` with a deprecation
warning, and keeps operating.  New clients SHOULD use the
`templated_units` key directly.

#### 5.1 Instance enumeration for templated units (v0.5)

When a client's `deploy.toml` declares `templated_units` (e.g.
`psk-recorder@.service`), sigmond discovers live instances and
expands the template for each one.

**Configured vs. known instances.** For each template:

- **Configured** = `{instance | /etc/<client-name>/env/<instance>.env
  exists}`.  This is the **authoritative** set sigmond will operate
  on for lifecycle verbs (start, stop, restart, reload).  The env
  file is the §4 configuration convention already used by all
  reference clients.
- **Known** = configured ∪ `{instance | systemctl list-units
  '<template>@*.service' --all reports it}`.  Known instances
  include orphans (instances running but with no env file —
  typically leftover from a removed instance).

**Lifecycle verb scope.**

- `smd start / stop / restart / reload <component>` (without explicit
  instance) operate on all **configured** instances of `<component>`.
- `smd list / status <component>` report on **known** instances,
  flagging any in (known − configured) as **orphaned**.  Orphans are
  running but absent from configuration — a signal of drift the
  operator should investigate.

**Env file convention.** All multi-instance clients MUST use:

```
/etc/<client-name>/env/<instance>.env
```

The `<instance>` part is matched against the systemd template
instantiation.  If a client has instances `default`, `lf`, and
`backup`:

```
/etc/psk-recorder/env/default.env       psk-recorder@default.service
/etc/psk-recorder/env/lf.env            psk-recorder@lf.service
/etc/psk-recorder/env/backup.env        psk-recorder@backup.service
```

#### 5.2 Lifecycle scope boundary (v0.5)

Sigmond's lifecycle verbs act **only** on the resolved union of:

- Concrete `units` (as-named).
- Instance-expanded `templated_units`.

**Out of scope:** auxiliary units a client ships but omits from the
arrays.  Examples: timers or failure handlers not in `units`, socket
units, oneshot units used for setup / teardown.  Clients manage
these via systemd `WantedBy=` / `PartOf=` relationships on
lifecycle-managed units, or via their own setup oneshot.

**Exception — targets with children:** if `units` declares a target
(e.g., `timestd-metrology.target`), sigmond expands it via
`systemctl list-dependencies --reverse <target>` before `stop`,
because `systemctl stop <target>` does not stop `Wants=` children.
Clients whose target contains lifecycle-managed children MUST use
`PartOf=<target>` on those children so stop propagates correctly.
Targets themselves are brought down last (after all `PartOf=` units).

#### 5.3 The `reload` verb and `ExecReload` convention (v0.5)

Clients MAY declare `ExecReload=/bin/kill -HUP $MAINPID` (or
equivalent) in their unit files to support the `smd reload
<component>` verb.

- `smd reload` maps to `systemctl reload <unit>` when `ExecReload`
  is present.
- Falls back to `systemctl try-restart` otherwise (reload-or-restart).

This is **distinct from** v0.5's `/reload` control-socket endpoint
(§13.3):

- `smd reload` is an OS-level signal to the unit.  Works without a
  control socket; needed for v0.5's log-level changes (§11).
- `POST /reload` on the control socket is an in-process config
  re-read with a structured response (which keys applied vs require
  restart).

**Optional auto-routing flag.** `smd reload --via=auto|systemd|socket`
(default: `auto`) prefers the control socket when `inventory --json`
reports a `control_socket` path (§3 amendment), falling back to
systemd if absent.

#### 5.4 Start / stop ordering (v0.5)

- **Start order:** components are started in the order they appear
  in `/etc/sigmond/coordination.toml`'s `[[clients.<name>]]` lists,
  with `radiod` always first.
- **Stop order:** reverse of start order.

**Cross-client dependencies.** Sigmond owns station-level ordering.
Clients MUST NOT declare cross-client `After=` / `Requires=`
dependencies beyond the implicit "radiod is upstream."  Operator
mistakes here (e.g. `psk-recorder` depends on `wspr-recorder`)
create fragile configurations sigmond cannot resolve.  Sigmond
validates `coordination.toml` and warns if suspicious cross-client
unit links are detected.

#### 5.5 Lifecycle lock and atomicity (v0.5)

Every mutating verb (`install`, `apply`, `start`, `stop`, `restart`,
`reload`, `update`) acquires an `flock` on:

```
/var/lib/sigmond/lifecycle.lock
```

This prevents concurrent apply-vs-restart races.  `list` and `status`
are lock-free readers.

### 6. Talking to radiod: use `ka9q-python` (revised v0.5)

Clients that consume RTP streams from a radiod instance **SHOULD**
use the `ka9q-python` library (`RadiodControl` etc.).  This is the
recommended default and provides the channel-reservation,
multicast-derivation, and teardown guarantees the suite expects to
hold across peers without per-client coordination work.  Re-pointing
a Path A client at a different radiod is just a `coordination.env`
rewrite.

Clients that manage their own radiod control connection MUST instead
satisfy §16, which defines the explicit obligations (multicast
non-collision, env-var resolution, chain-delay application, clean
teardown) that allow such a client to remain sigmond-manageable.
Such clients declare themselves with
`data_path.kind = "radiod-direct"` in `inventory --json` (§16.3).

Clients whose data source is not a radiod instance at all (KiwiSDR,
file replay, etc.) are out of scope for §6 entirely; see §16.5.

**Why this changed from v0.4.**  The v0.4 prohibition (MUST NOT
speak radiod's protocol directly) was unenforceable by sigmond at
runtime — sigmond does not inspect radiod's control socket.  The
rule conflated two different concerns:

- **Sigmond's runtime invariants** — non-colliding multicast,
  env-var-driven status DNS, applied chain delay, clean teardown.
  These remain MUST via §16.4 for `radiod-direct` clients; §16.5
  governs non-radiod clients.
- **Project-coordination conventions** — uniform implementation,
  shared bug-fix surface, coordinated evolution with `ka9q-radio`.
  These remain SHOULD with §16 as the explicit opt-out path; §16.6
  documents the trade Path B clients accept.

The change is **purely additive** with respect to v0.4 conformance:
v0.4 clients that use `ka9q-python` remain conformant under v0.5
unchanged; sigmond infers `data_path.kind = "radiod-ka9q-python"`
when the field is absent (§16.3).  Only clients adopting Path B
need to publish `data_path` explicitly.

### 7. Deterministic data multicast destination (v0.2, revised v0.3, implemented v3.14.0)

**Rule.** Every client that subscribes to radiod RTP data streams MUST
use `ka9q-python`'s `RadiodControl.ensure_channel()` for channel
creation.  The client MUST construct `RadiodControl(...)` with a
`client_id="<stable-client-name>"` kwarg.  Clients MUST NOT pass a
`destination=` argument to `ensure_channel()`.  `ka9q-python`
(≥ 3.14.0) derives the multicast destination deterministically from
`(client_id, status_address)` via `generate_multicast_ip()` and
returns the resolved address in `ChannelInfo`.  Clients read this
value for `inventory --json` reporting but never select or compute
it.

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

**Why v3.14.0 is the floor.**  The v0.3 contract revision shipped
2026-04-12.  ka9q-python's `ensure_channel()` accepted a `destination=`
kwarg but did not derive one when the caller omitted it, so for the
year between v0.3 and v3.14.0 every client on a given radiod silently
landed on radiod's config-file default group — exactly the failure
mode v0.2 was written to prevent.  ka9q-python 3.14.0 closes that gap
by adding `RadiodControl(client_id=...)`; clients opt in once at
construction time.  An `ensure_channel` call without an explicit
`destination=` *and* a `RadiodControl` built without `client_id=`
retains pre-3.14 behaviour for rollback safety.

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

**Relationship to §18 (v0.7).**  §8 is the *static* hardware-pipeline
correction (fixed ns offset per radiod analog/ADC path).  §18 is the
*dynamic* timeline-anchor correction (epoch + rate, refreshed per
authority cycle).  The two compose and address different errors;
applied together:

```
utc_final_ns = utc_via_§18(rtp_sample_n) − chain_delay_ns_§8
```

§18 corrects the radiod host clock's contribution to RTP→UTC; §8
corrects the analog-front-end → ADC pipeline delay.  Neither replaces
the other.  See §18.6.

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

**v0.5 retrofits (2026-05-04).**  All conformant clients
declare `data_path` (§16.3) and bumped `contract_version` to
`"0.5"`:

- **psk-recorder, hf-timestd, wspr-recorder** — all on Path A,
  declare `kind = "radiod-ka9q-python"`.  Each adds `control_socket`
  (§3 amendment + §13.1) per instance; the socket itself is
  advisory until §13's server is implemented.

The first end-to-end §16.4 (Path B / `radiod-direct`) candidate is
David Goncalves' RX888 WebSDR-style client (early discussion as of
2026-05-04) — it will be the first real exercise of §16.4's
collision-avoidance, env-var resolution, chain-delay application,
and clean-teardown obligations.

If a retrofit or greenfield build uncovers a gap between the
contract as written here and what the reference clients ship, fix
the contract — update this document and bump the version — rather
than adding a per-client special case to sigmond's
`ContractAdapter`.

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

### 13. Control surface (v0.5)

Each running client exposes a uniform runtime view that sigmond and
`smd status` / `smd diag` can read without per-client knowledge.
This complements §3's `inventory --json` (the static, what-could-be
view) with a live, what-is-happening view.

#### 13.1 Transport

Each running client MUST expose an HTTP/JSON endpoint over a
**unix-domain socket** at:

```
/run/<client-name>/control.sock                  # single-instance
/run/<client-name>/<instance>.control.sock       # multi-instance
```

The socket is created mode `0660`, owned by the client's service
user, group `sigmond` (created by sigmond install).  Sigmond and
the client's own operator can read; nobody else.

Implementation in stdlib only:
`http.server.BaseHTTPRequestHandler` bound to a
`socketserver.UnixStreamServer`.  No third-party web framework
dependency for conformance.

`curl --unix-socket /run/<client>/control.sock http://./status`
MUST work for headless debugging.  This is the headless-first
equivalent of opening a TUI panel and is the property that keeps
the contract debuggable from SSH.

**Why unix sockets, not TCP / MQTT.**  Keeps v0.5 single-host: no
broker dependency, no auth/TLS in scope, no port collisions on
hosts running multiple radiod + multiple clients.  The socket path
is the identity.  Multi-host aggregation (LAN, possibly via SSH
tunnel or an optional MQTT bridge sidecar) is a v0.6+ concern; the
schema below is designed to survive that promotion unchanged.

#### 13.2 Mandatory endpoints

| Method | Path        | Purpose |
|--------|-------------|---------|
| GET    | `/healthz`  | Liveness. 200 if process is up and event loop responsive. |
| GET    | `/readyz`   | Readiness. 200 only if input is flowing AND output path (spool / upload / etc.) is writable. |
| GET    | `/status`   | One-shot snapshot. Schema in §13.4. |
| GET    | `/metrics`  | Prometheus text format. Counters and gauges from §13.4 plus client-specific extras. |

Mandatory endpoints MUST respond in <100 ms under nominal load and
MUST NOT block on I/O against radiod, the network, or downstream
consumers — they read cached state updated by the client's own
loop.

#### 13.3 Optional endpoints (recommended where applicable)

| Method | Path                            | Purpose |
|--------|---------------------------------|---------|
| GET    | `/channels`                     | List per-channel state (FT8 channels, WSPR bands, hf-timestd outputs). |
| GET    | `/channels/{id}`                | Per-channel detail. |
| POST   | `/channels/{id}/enable`         | Runtime enable, no config edit. |
| POST   | `/channels/{id}/disable`        | Runtime disable. |
| GET    | `/events?since=<seq>&limit=<n>` | Ring buffer of structured events (decode, drop, upload-fail, IGMP-rejoin). |
| POST   | `/reload`                       | Re-read config. Body: `{"dry_run": bool}`. Response lists keys applied vs keys requiring restart. |

Clients MAY add further endpoints under `/x/<client-name>/...` for
client-specific debug.  Sigmond never depends on `/x/...`.

#### 13.4 `/status` JSON schema

```json
{
  "service":  "psk-recorder",
  "instance": "default",
  "version":  "0.2.0",
  "contract": "0.5",
  "state":    "running",          // running | degraded | stopped | starting
  "uptime_s": 12345,
  "pid":      4711,

  "radiod": {
    "id":      "bee3-rx888",
    "status_addr": "bee3-rx888-status.local",
    "samprate":    12000,
    "last_status_rx_age_s": 0.4
  },

  "multicast": {
    "groups_joined": [
      {"group": "239.100.112.151", "port": 5004, "iface": "eth0",
       "last_pkt_age_s": 0.02, "pps": 12000,
       "last_igmp_report_age_s": 41.2}
    ],
    "drops_1m": 0,
    "out_of_order_1m": 0
  },

  "channels": [
    {"id": "14074-ft8", "enabled": true, "ssrc": 14074000,
     "decodes_15m": 42, "last_decode_age_s": 7.1}
  ],

  "spool": {
    "dir":            "/var/lib/psk-recorder/bee3-rx888/ft8",
    "fs":             "ext4",
    "depth_files":    3,
    "oldest_age_s":   12,
    "bytes_written_1m": 5242880,
    "writable":       true
  },

  "pipeline": {
    "lag_s":           0.4,
    "queue_depth":     0,
    "last_success_age_s":  3.2,
    "last_error":     null,
    "backpressure":   false
  },

  "resources": {
    "cpu_pct_1m":   8.4,
    "rss_mb":       142,
    "open_fds":     37
  },

  "log_paths": {                  // mirrors §10
    "stderr": "journal:psk-recorder.service",
    "files":  []
  }
}
```

**Field rules.**

- `state = degraded` MUST be set if any of: no input packets in
  >2× expected interval, spool not writable, pipeline backpressure
  asserted, or downstream `last_success_age_s` exceeds a
  client-defined threshold.
- All `*_age_s` fields are seconds since the named event — avoids
  client/server clock-skew issues that absolute timestamps cause.
- A field that does not apply to a given client MAY be omitted
  entirely (e.g. hf-timestd has no `spool`; wspr-recorder has no
  per-channel `decodes_15m`, it has `spots_15m`).
- The schema is **additive**: clients MAY include extra keys;
  sigmond and TUI widgets MUST ignore unknown keys.

#### 13.5 Mapping to existing clients

- **`psk-recorder`** — exposes most of this internally; needs the
  socket server and JSON marshalling.  `pipeline` is the decoder
  (`jt9` / `wsjtx`) lag and PSKReporter upload queue.
- **`wspr-recorder`** — `channels` becomes the band list,
  `decodes_15m` becomes `spots_15m`, `pipeline` is the
  downstream decoder handoff (spool depth IS the queue, so
  `queue_depth = spool.depth_files`; `last_success_age_s` is the
  newest deletion from spool, observed by inotify or stat).
- **`hf-timestd`** — keeps its existing web API.  v0.5 adds the
  unix socket as a *parallel* surface that reports only the
  inter-client slice (multicast groups, output writable, BPSK PPS
  calibration status).  The web API remains authoritative for the
  science and for deep debug.  Sigmond reads only the unix socket.

#### 13.6 Inter-client effects sigmond can detect from §13.4 alone

These are concrete cross-client conditions sigmond can flag without
reaching into any client's internals:

1. **Multicast group collision** — two clients on the same host
   reporting the same `multicast.groups_joined[].group` when they
   should not.
2. **IGMP-snooping silent failure** — `last_pkt_age_s` is small
   but `last_igmp_report_age_s` is climbing past the switch's
   query interval; classic ka9q-radio gotcha.
3. **Shared-spool exhaustion** — multiple clients writing to the
   same `spool.fs`, aggregate `bytes_written_1m` rising,
   `oldest_age_s` on a downstream consumer climbing.
4. **CPU budget breach** — sum of `resources.cpu_pct_1m` across
   clients on one host exceeds the budget sigmond allocated.
5. **Radiod loss** — multiple clients on the same `radiod.id` all
   reporting `last_status_rx_age_s` climbing in lockstep ⇒ radiod
   is the fault, not any one client.
6. **Back-pressure cascade** — one client's
   `pipeline.backpressure = true` correlated with peer's
   `multicast.drops_1m` rising.

None of these require sigmond to know what any client *does*
internally — only what it exposes at the boundary.

#### 13.7 Scope discipline

- **Not a config API.** `/reload` re-reads the on-disk config file
  written by the operator (or by sigmond drop-in for
  `coordination.env`).  It does not accept config payloads.  Config
  authoring stays out of the runtime surface.
- **Not multi-host.** Single-host unix socket only.  Multi-host is
  v0.6+ (probably an opt-in MQTT bridge or SSH-tunnel aggregator
  that consumes `/status` and republishes).
- **Not a debug API.** Per-client deep state stays in the client's
  own surface.  Sigmond reads only the boundary.
- **Not authenticated.** Filesystem permissions on the socket are
  the authn boundary.  If the surface ever leaves the host, the
  bridge layer owns auth, not the contract.

### 14. Configuration interview (v0.5)

Each client owns its config schema and its config UX (a wizard, an
interactive editor, a templated TOML — whatever the client author
prefers).  Sigmond does not write inside a client's config files
(reaffirming §1).  But sigmond **does** know about values that span
clients — the operator's callsign and grid square, which radiod a
client should bind to, whether an `hf-timestd` instance is present
that other clients can reference for timing — and it should offer
those as defaults so the operator doesn't type the same callsign
into five different wizards.

This section adds two things:

1. A `[contract.config]` block in `deploy.toml` that lets a client
   advertise the entry points sigmond should invoke for guided
   configuration.
2. A stable **env var bag** sigmond passes to those entry points,
   carrying the cross-client commons.

#### 14.1 `deploy.toml` block

```toml
[contract.config]
init = "scripts/setup-station.sh"           # string form (single executable)
edit = "scripts/config-review.sh"

# OR — argv list form, for clients that route through subcommands:
[contract.config]
init = ["/usr/local/bin/psk-recorder", "config", "init"]
edit = ["/usr/local/bin/psk-recorder", "config", "edit"]
```

- **String form** — a path to an executable in any language;
  sigmond spawns it directly.  Paths are relative to the repo
  root, or absolute.
- **Argv form** — a list whose first element is the executable and
  whose remaining elements are pre-pended arguments.  Useful when
  a client exposes its configurator as a subcommand of its main
  CLI rather than as a separate script.
- Both keys are optional.  A client may provide one and not the
  other (e.g. only `init`, leaving subsequent edits to direct file
  editing).
- When a key is absent, sigmond's fallback (§14.4) applies.

#### 14.2 Invocation surface

Sigmond exposes:

```
smd config init <client> [<instance>]   invoke [contract.config].init
smd config edit <client> [<instance>]   invoke [contract.config].edit
```

The optional `<instance>` argument names a specific instance for
multi-source clients.  A station running, e.g., two `wspr-recorder`
instances bound to different radiod sources can configure each
independently:

```
smd config init wspr-recorder radiod-0    # bound to local radiod
smd config init wspr-recorder radiod-1    # bound to remote radiod
```

When `<instance>` is omitted, sigmond does not set
`SIGMOND_INSTANCE`; clients that don't model multi-instance can
ignore the variable.  When provided, sigmond sets
`SIGMOND_INSTANCE` to the literal name and resolves
`SIGMOND_RADIOD_STATUS` from the specific `[[clients.<name>]]`
entry whose `instance` field matches (via its `radiod_id`).  The
client decides what to do with the value — some clients (e.g.
`psk-recorder`) carry multiple `[[radiod]]` blocks in a single
config file; others (e.g. `wspr-recorder`, `hfdl-recorder`) split
per-instance config under `/etc/<client>/<instance>/`.

The script inherits sigmond's stdin/stdout/stderr (no redirection),
runs in the current TTY, and returns its exit code as the verb's
exit code.  Clients SHOULD also accept `--non-interactive` so
sigmond's TUI can drive the same script without a controlling
terminal (TUI integration is post-v0.5).

#### 14.3 Env var bag

When invoking `init` or `edit`, sigmond sets the following env vars
in addition to the existing inherited environment.  All are
**advisory**: the script SHOULD use them as prompt defaults, never
as authoritative overrides.

| Variable | Source | Meaning |
|---|---|---|
| `STATION_CALL`         | `coordination.toml [host].call`           | Operator callsign — bare, no suffix |
| `STATION_GRID`         | `coordination.toml [host].grid`           | Maidenhead grid square |
| `STATION_LAT`          | `coordination.toml [host].lat`            | Latitude (decimal degrees) |
| `STATION_LON`          | `coordination.toml [host].lon`            | Longitude (decimal degrees) |
| `SIGMOND_INSTANCE`     | the `<instance>` arg from the verb         | Set only when invoked with an instance.  Clients deriving per-instance fields consume this. |
| `SIGMOND_RADIOD_COUNT` | number of `[radiod.<id>]` blocks in coordination.toml | Always set.  `1` is the simple case where reporter IDs need no per-instance suffix; `>1` signals that distinct reporter IDs per radiod are required (see §14.6). |
| `SIGMOND_RADIOD_INDEX` | 1-based position of this instance's radiod in coordination.toml declaration order | Set only when `<instance>` is given and resolves to a known radiod.  Lets clients compose stable per-radiod suffixes without parsing `SIGMOND_INSTANCE`. |
| `SIGMOND_RADIOD_STATUS`| `coordination.toml [radiod.<id>].status_dns` — see resolution rule below | radiod multicast status DNS this client/instance should bind to |
| `SIGMOND_TIME_SOURCE`  | hf-timestd inventory if installed, else NTP from `environment.toml` | `<kind>@<host>:<port>` — e.g. `hf-timestd@localhost:8000` |
| `SIGMOND_GNSS_VTEC`    | hf-timestd inventory `commons.gnss_vtec` (§14.5) when present | `<host>:<port>` — surfaced for clients that consume ionospheric data |

**`SIGMOND_RADIOD_STATUS` resolution:**

1. If `<instance>` is given and `coordination.toml` contains a
   `[[clients.<client>]]` entry whose `instance` matches and whose
   `radiod_id` resolves to a `[radiod.<id>]` block, use that
   block's `status_dns`.
2. Else if exactly one `[radiod.<id>]` block is declared, use its
   `status_dns`.
3. Else leave the var unset (operator picks interactively).

The bag is intentionally small.  Adding a new variable is a minor
contract bump; clients opt in by reading the new name.  The first
four already exist in `coordination.env` (consumed at runtime via
systemd `EnvironmentFile=-`); §14 just guarantees they're also
present at config-time.

#### 14.4 Fallback when no `[contract.config]`

If a client's `deploy.toml` has no `[contract.config]` block,
sigmond:

- For `smd config init <client>`: prints the path of the rendered
  template (from `[install]` `kind = "render"`, §5) and exits.  No
  interactive flow.
- For `smd config edit <client>`: opens `$EDITOR` (default `vi`)
  on the deployed config path reported by `inventory --json`
  (`config_path`).

This keeps `smd config edit` useful day one for every contract-
conformant client, even before they ship a wizard.

**Special case — radiod.**  radiod (`ka9q-radio`) is the upstream
that HamSCI clients consume from, not itself a HamSCI contract
client, so it has no `deploy.toml [contract.config]`.  Sigmond owns
radiod's configuration directly: `smd config init radiod` runs a
built-in wizard that probes the local USB bus, prompts the
operator for an instance id / status DNS / antenna description per
detected SDR, and renders `/etc/radio/radiod@<id>.conf` from
`etc/radiod.conf.template`.  Each rendered config locks to a
specific physical SDR via the `serial = "..."` key recognised by
every front-end driver (rx888, airspy, airspyhf, sdrplay).  The
wizard then appends a `[radiod."<id>"]` block to
`coordination.toml`, which makes the new radiod immediately
visible to the rest of the configurations contract
(`SIGMOND_RADIOD_COUNT`, `SIGMOND_RADIOD_INDEX`, per-instance
`SIGMOND_RADIOD_STATUS`).  This honors the workflow ordering of
§14.6: radiod gets configured *before* clients that consume from
it.

#### 14.5 Inventory contributions (forward-compatible hook)

A v0.5+ client MAY add a `commons` block to `inventory --json`
reporting the values it currently has set for variables in the
§14.3 bag.  This is the basis for `smd validate` to detect drift
between a client's stored config and `coordination.toml [host]`.
Drift becomes a validation warning, never a silent rewrite —
sigmond never edits client files.

```json
{
  "contract_version": "0.5",
  "config_path": "/etc/hf-timestd/timestd-config.toml",
  "commons": {
    "station_call":  "AC0G",
    "station_grid":  "EM38",
    "gnss_vtec":     "192.168.1.50:2123"
  }
}
```

A client MAY also include entries that contribute to other clients'
view of the environment — e.g. an installed hf-timestd advertises
its GNSS-VTEC endpoint, which sigmond surfaces as
`SIGMOND_GNSS_VTEC` when invoking other clients' `init` / `edit`.

#### 14.6 Workflow ordering and reporter naming

Sigmond's situational-awareness inventory (`smd environment`) and
the operator's `coordination.toml` together establish *which
radiods exist* before any client is configured.  The intended
workflow is:

1. **Discover** — `smd environment probe` finds reachable peers.
2. **Declare** — operator records radiod(s) in
   `coordination.toml [radiod.<id>]`.
3. **Configure** — `smd config init|edit <client> [<instance>]`
   runs, with the env bag populated from the now-known
   coordination state.

Because of this ordering, by the time a client's interview runs,
`SIGMOND_RADIOD_COUNT` is authoritative.

**Reporter-naming convention (per-client).** The reporter ID a
client sends to its upstream service (PSK Reporter, WSPR Net,
airframes.io) generally derives from `STATION_CALL`.  When exactly
one radiod is declared, no per-radiod suffix is needed and
`STATION_CALL` is used verbatim.  When more than one is declared,
the client should suffix the call to disambiguate which receive
setup produced the report.  The suffix format is per-client
convention:

| Client          | Single radiod | Multi radiod (n = `SIGMOND_RADIOD_INDEX`) |
|---|---|---|
| psk-recorder    | `AC0G`        | `AC0G/B<n>`     (e.g. `AC0G/B1`, `AC0G/B2`) |
| wspr-recorder   | `AC0G`        | `AC0G/B<n>`     (e.g. `AC0G/B1`, `AC0G/B2`) |
| hfdl-recorder   | `AC0G-1`      | `AC0G-<n>`      (airframes.io requires the suffix even for single) |

Sigmond does not enforce these conventions — each client picks its
default in `config init` and the operator may override
interactively.  The contract's job is to surface the env bag
(`SIGMOND_RADIOD_COUNT`, `SIGMOND_RADIOD_INDEX`, `STATION_CALL`)
so clients have what they need to compose a sensible default.

### 15. Radiod channel contributions (v0.5)

Radiod is upstream of every HamSCI client: clients consume
multicast streams from `radiod@<id>`, but they don't run radiod
themselves.  Most clients also need radiod to *create* a multicast
channel for them (WSPR, FT4, FT8, HFDL, WWV/CHU, ...) — that's
typically a small section appended to `radiod@<id>.conf` or, more
cleanly, a fragment file in `radiod@<id>.conf.d/`.

Before v0.5 each client's `install.sh` wrote its own fragment by
hand.  Conventions diverged, the operator had to re-run client
installers whenever a new radiod instance was provisioned, and
there was no sigmond-side awareness of who-contributes-what.  v0.5
gives clients a declarative way to say "I need this channel on
these radiods" — sigmond applies the result.

#### 15.1 Declaration

A client author adds zero or more `[[radiod.fragment]]` blocks to
its `deploy.toml`:

```toml
[[radiod.fragment]]
priority = 30                                # NN in <NN>-<client>.conf (00-99)
target   = "${RADIOD_ID}"                    # "*", a literal id, or ${VAR}
template = "etc/radiod-fragment.conf"        # path inside the client repo
```

| Field      | Required | Notes                                                                      |
|------------|----------|----------------------------------------------------------------------------|
| `priority` | no       | Default 50.  Smaller = earlier in radiod's load order.                     |
| `target`   | no       | Default `"*"` (every declared radiod).  May be a literal id or `${RADIOD_ID}`.|
| `template` | yes      | Path inside the repo.  `${VAR}` interpolation against the variable bag below. |

The legacy spelling `content_template` is accepted as an alias for
`template` so prototype clients don't have to flag-day rename.

#### 15.2 Variable bag

Templates are rendered with stdlib `string.Template` (`${VAR}`
interpolation).  Unknown variables are left in place as literal
`${UNKNOWN}` — `safe_substitute` semantics — so a typo is visible
at the output rather than crashing the apply.

| Variable          | Source                                       |
|-------------------|----------------------------------------------|
| `RADIOD_ID`       | the target radiod instance id                |
| `RADIOD_HOST`     | `coordination.toml` `[[radiod]] host`        |
| `RADIOD_STATUS`   | `coordination.toml` `[[radiod]] status_dns`  |
| `RADIOD_SAMPRATE` | `coordination.toml` `[[radiod]] samprate_hz` |
| `STATION_CALL`    | `coordination.toml` `[host] call`            |
| `STATION_GRID`    | `coordination.toml` `[host] grid`            |
| `STATION_LAT`     | `coordination.toml` `[host] lat`             |
| `STATION_LON`     | `coordination.toml` `[host] lon`             |

#### 15.3 Apply path

Sigmond writes each rendered fragment to:

```
/etc/radio/radiod@<id>.conf.d/<NN>-<client>.conf
```

The applier runs:

1. During `smd apply`, before the radiod ensure-running block — so
   fragments are in place when each radiod instance starts (or
   restarts) and picks up its `conf.d/` contents.
2. During `smd config init radiod`, scoped to the freshly-created
   instance — so a brand-new radiod inherits every enabled
   client's fragments without a separate command.

The apply is idempotent (sha256 compare against existing content)
and supports `--dry-run`.  Failures (missing template,
unparseable deploy.toml, target id not declared in
coordination.toml) degrade into `warning:`-prefixed status lines;
the rest of `smd apply` continues.

#### 15.4 Target resolution

| `target`           | Behaviour                                              |
|--------------------|--------------------------------------------------------|
| `"*"`              | every declared radiod                                  |
| `"${RADIOD_ID}"`   | every declared radiod (variable filled in per-write)   |
| `"<literal-id>"`   | only that radiod, if declared in coordination.toml     |
| anything else      | `warning:` line, no write                              |

A fragment that does not resolve to any declared radiod produces a
warning.  This is intentional — silently dropping the contribution
would mask a misconfigured client.

#### 15.5 Migration

Clients that today install fragments via their own `install.sh`
should:

1. Move the fragment body to a template file in the repo (e.g.
   `etc/radiod-fragment.conf`).
2. Rewrite the parts that depend on `RADIOD_ID` / call / grid /
   etc. as `${VAR}` placeholders.
3. Add a `[[radiod.fragment]]` block to `deploy.toml`.
4. Drop the fragment-write code from `install.sh` — it becomes
   sigmond's responsibility.

Sigmond's apply is idempotent, so a half-migrated client
(`install.sh` still writes the same fragment that sigmond would
write) keeps working during the transition.  Once `install.sh`
stops writing, future operator edits to the conf.d/ file will be
replaced on the next `smd apply` — which is the right behaviour:
the deploy.toml is now the source of truth.

### 16. Independent data-source clients (v0.5)

#### 16.1 Scope

Two real cases motivate this section:

1. **Direct-radiod clients** — a client that consumes radiod RTP
   but manages its own control connection rather than going
   through `ka9q-python`.  Reasons include: an existing codebase
   that already speaks radiod's protocol, a non-Python client, or
   a client whose internal lifecycle is incompatible with
   `RadiodControl`'s reservation model.
2. **Non-radiod clients** — a client whose data source is not a
   radiod instance at all (KiwiSDR audio, file replay, a different
   SDR daemon, pure analytics on archived data).  For these
   clients §2, §6, §7, and §8 do not apply.

This section defines the obligations that make either case
contract-conformant for sigmond's install / lifecycle / monitoring
purposes.  §6 (revised v0.5) recognises §16 as the explicit
opt-out path from the `ka9q-python` recommendation.

#### 16.2 Why this exists

Sigmond's actual runtime view of a client is exactly four things:
the systemd unit state, `<client> inventory --json`, `<client>
validate --json`, and the v0.5 control surface (§13).  Sigmond
does not inspect radiod's control socket, does not parse ka9q's
wire protocol, and does not know which library produced a client's
multicast traffic.  The v0.4 §6 prohibition was therefore
**normative, not enforced** — a project-coordination convention
rather than a sigmond runtime invariant.  v0.5 makes this honest
by defining the alternative path explicitly rather than leaving
non-conforming-but-functional clients in a documentation gray
zone.

The benefits `ka9q-python` provides remain real (consistent
teardown, shared evolution of the multicast derivation formula,
automatic collision-avoidance, single bug-fix surface).  Path A
(ka9q-python) is the recommended default.  Path B (independent)
is for clients that have a concrete reason to manage their own
data plane.

#### 16.3 Self-disclosure: `data_path` in inventory

Every instance entry in `<client> inventory --json` MUST include a
`data_path` object that names how the client receives samples:

```json
{
  "instance": "default",
  "data_path": {
    "kind": "radiod-ka9q-python",
    "radiod_id": "bee3-rx888"
  }
}
```

`kind` is one of:

| `kind`                | Meaning                                                  |
|-----------------------|----------------------------------------------------------|
| `radiod-ka9q-python`  | Standard path. Client uses `RadiodControl`. (§6 default) |
| `radiod-direct`       | Client speaks radiod's control protocol itself.          |
| `kiwisdr`             | Client consumes audio/IQ from a KiwiSDR.                 |
| `file`                | Client reads WAV/IQ files from disk. **With** `details.upstream_client`: meta-client whose data is spooled by a sibling sigmond client (§16.3.1). **Without**: replay/test data from an archive. |
| `other`               | Anything else; `details.description` SHOULD explain.     |

`details` is an optional object whose schema is `kind`-specific.
Sigmond MUST treat unknown `kind` values as `other` and continue.

For backward compatibility, sigmond MUST treat a missing
`data_path` field on a v0.4-conformant client as
`kind = "radiod-ka9q-python"` — that was the only conformant
option before §16.

#### 16.3.1 Meta-clients: `kind = "file"` with `details.upstream_client`

A meta-client is a client whose own data plane is files spooled by
**another** sigmond-managed client.  It does not open a radiod
control connection or KiwiSDR socket; it consumes the WAV/IQ
stream a sibling client has already written to disk.

A decoder that reads another recorder's WAV spool is the canonical
example: each instance reads WAVs from an upstream recorder client
(e.g. `wspr-recorder`, declaring `radiod-ka9q-python`) and
decodes/posts them.  The radiod-side facts (radiod_id, multicast
destination, chain delay) live on the upstream client's inventory,
not the meta-client's.

A meta-client MUST declare `kind = "file"` per instance with
`details.upstream_client = "<sibling-client-name>"` plus enough
context for an operator to find the upstream's inventory:

```json
{
  "instance": "ka9q_0-20",
  "data_path": {
    "kind": "file",
    "details": {
      "upstream_client": "wspr-recorder",
      "upstream_unit":   "wd-ka9q-record@KA9Q_0.service",
      "spool":           "/var/spool/wsprdaemon/recording/KA9Q_0"
    }
  }
}
```

Sigmond MAY cross-reference `details.upstream_client` against the
catalog and the upstream client's `inventory --json` output to
populate views that join "what's decoding" with "what radiod
consumed it from."  A meta-client is not on the hook for §16.4's
radiod-direct obligations — those are satisfied by the upstream
client, where they actually exist.  If `details.upstream_client`
does not name a known catalog entry, sigmond SHOULD surface a
`warn`-level issue: the meta-client has named a sibling sigmond
does not know about, which is recoverable but odd.

This is distinct from `kind = "file"` for **archived/replay** data
(test fixtures, captured IQ files, post-mortem analysis).  For
replay clients, `details` SHOULD describe the source dataset (path
glob, capture date, source description) and SHOULD NOT include
`upstream_client`.  Sigmond uses the presence/absence of
`upstream_client` to disambiguate the two cases.

#### 16.4 Obligations for `radiod-direct` clients

A direct-radiod client MUST:

1. **Bind by radiod id** (§2 unchanged).  The `radiod_id` field
   appears in inventory; status DNS resolves via
   `RADIOD_<id>_STATUS` from `coordination.env`, with a
   `radiod_status` fallback for standalone operation.
2. **Pick a non-colliding multicast destination.** Sigmond's
   collision check (§7) reads `data_destination` from inventory
   and flags duplicates across peers.  The client is responsible
   for the picking; sigmond is responsible for detecting the
   collision and surfacing it through `smd diag`.  Acceptable
   picking strategies:
   - Read what the running radiod assigned and report it
     (recommended).
   - Use the same derivation formula `ka9q-python` uses, kept in
     sync by the client author.
   - Operator-provided destination in the client's native config.
3. **Report `data_destination`** truthfully in inventory, exactly
   as §7's existing surface requires.
4. **Honor `RADIOD_<id>_CHAIN_DELAY_NS`** from `coordination.env`
   on startup and SIGHUP, and report the applied value as
   `chain_delay_ns_applied` in inventory (§8 unchanged).  This is
   a plain env-var read; `ka9q-python` is not required.
5. **Implement clean teardown.** When stopped, the client MUST
   release any radiod-side reservation it holds.  Stale
   reservations that survive a restart cycle are a contract
   violation.
6. **Document the choice.** The client's README SHOULD explain
   why this client uses `radiod-direct` rather than
   `radiod-ka9q-python`.  Project hygiene, not a `validate` check.

A direct-radiod client SHOULD NOT reimplement `ka9q-python`'s wire
protocol if a thin wrapper around an existing radiod CLI tool
would do — the maintenance-surface argument is real.

#### 16.5 Obligations for non-radiod clients

A non-radiod client (`kind` ∈ `kiwisdr`, `file`, `other`):

- MUST omit `radiod_id`, `data_destination`, and
  `chain_delay_ns_applied` from inventory (these would be
  misleading).
- MUST populate `data_path.details` with enough information for
  an operator to understand the data source (e.g. KiwiSDR
  hostname, file glob, source description).
- MAY participate in §10 (log paths), §11 (runtime log level),
  §13 (control surface), §14 (configuration interview), and §18
  (timing authority, via station-wide discovery and the host-clock
  bridging formula in §18.5) on equal footing with radiod clients
  — none of those surfaces depend on radiod.
- §2, §6, §7, §8 do not apply.

Non-radiod clients in §18 authority-corrected mode MUST report
`timing_authority_applied` (§3) like any other subscriber.  The
field's `radiod_id` MUST be omitted (or set to `null`); the
`source`, `tier`, `sigma_ns`, and `snapshot_age_s` fields are
reported as usual.  This is not in tension with the "MUST omit
radiod fields" rule above: `timing_authority_applied` describes
the *authority subscription*, not a radiod relationship — the
authority happens to also annotate radiod streams for other
subscribers, but a non-radiod client is using its station-wide
service.

#### 16.6 What independent clients give up

Documenting the trade so the choice is informed:

- **Coordinated evolution.** When `ka9q-python` tracks an upstream
  ka9q-radio change in control protocol, multicast derivation, or
  teardown semantics, Path A clients get the fix for free.  Path
  B clients become a maintenance island.  `smd ka9q-watch` flags
  upstream drift but cannot patch your client.
- **PyPI version discipline (§12.6).** The `ka9q-python` PyPI-lag
  check doesn't help a client that doesn't depend on
  `ka9q-python`.  Direct-radiod clients SHOULD include an
  equivalent version-pin check in `validate --json` against
  whatever they do depend on.
- **Default collision avoidance.** Path A clients get this for
  free via per-client-identity derivation.  Path B clients must
  do it on purpose, and the contract trusts them to.
- **§13 widget detail.** Sigmond's TUI widgets read fields from
  `/status` (§13.4).  A non-radiod client's `/status` has empty
  `multicast` and `radiod` blocks; widgets degrade gracefully but
  the operator sees less detail.

#### 16.7 Sigmond's view

Sigmond MUST NOT distinguish between `data_path.kind` values for
the purposes of install (§5.0), lifecycle (§5.1–§5.5), logging
(§10–§11), control surface (§13), or configuration interview
(§14).  All clients that report a valid `inventory --json` and
`validate --json` are equally manageable.

Sigmond MAY use `data_path.kind` to:

- Skip radiod-specific harmonization rules for non-radiod clients.
- Annotate `smd status` output (e.g. `psk-recorder
  [radiod-ka9q-python]` vs. `kiwi-monitor [kiwisdr]`).
- Decide whether `RADIOD_<id>_*` env-var checks apply to a given
  client when validating `coordination.env`.

### 17. Output sinks (v0.6)

#### 17.1 Scope

§16 defines how a client receives samples (`data_path`).  §17 defines
how a client emits **derived data** — decoded spots, frames,
measurements — to operator-visible storage.  Two real shapes exist
today:

1. **File sinks.**  A client writes log lines, JSON, HDF5, or any
   other format to a path in `/var/lib/<client>/`,
   `/var/log/<client>/`, or `/var/spool/<client>/`.  This is the
   default and the only shape every conformant client supports.
2. **Structured-row sinks.**  A client writes structured rows to a
   local staging tier via a producer-side sink library
   (`sigmond.hamsci_sink.Writer.from_env()`).  The backend is
   SQLite (`/var/lib/sigmond/sink.db`).  Rows are later read by the
   separate `hs-uploader` library and shipped to upstream
   destinations (e.g. psws.eng.ua.edu).

This section makes the surface declarative so sigmond can budget
disk and surface backpressure in `smd diag`.

#### 17.2 Why this exists

Through v0.5, output paths were inventory-by-convention: §3's
`disk_writes` array told sigmond about file outputs, and that was
enough because every conformant client was file-only.  The
introduction of a local structured-row staging tier creates a
second output kind that needs the same disk-budget and
operator-visibility surface that `disk_writes` already provides.

Rather than fork "files vs. service" into two parallel inventory
arrays, §17 unifies them under a single `data_sinks` array
parametrized by `kind`.  This is the symmetric counterpart to
§16's `data_path` (input).  Backwards compatibility is
unconditional: a v0.5 client that declares only `disk_writes` is
auto-promoted by sigmond into the equivalent `data_sinks` shape
(§17.4).

#### 17.3 Self-disclosure: `data_sinks` in inventory

A v0.6 client SHOULD report `data_sinks` per instance:

```json
{
  "instance": "default",
  "data_sinks": [
    {
      "kind":           "file",
      "target":         "/var/log/psk-recorder/spots-default.log",
      "schema_ref":     null,
      "retention_days": 14,
      "mb_per_day":     12
    },
    {
      "kind":           "service",
      "target":         "psk.spots",
      "schema_ref":     "psk:3",
      "retention_days": 30,
      "mb_per_day":     8,
      "health":         "ok"
    }
  ]
}
```

Each entry has:

| field            | type    | meaning                                                          |
|------------------|---------|------------------------------------------------------------------|
| `kind`           | string  | One of `file`, `service`.  Unknown kinds: see §17.6.             |
| `target`         | string  | Path (for `file`) or `<database>.<table>` (for service sinks).   |
| `schema_ref`     | string  | `<db>:<schema_version>` for service sinks; `null` for `file`.    |
| `retention_days` | integer | How long the client expects rows/bytes to stay accessible.       |
| `mb_per_day`     | integer | Best-effort write rate; consumed by `[disk_budget]`.             |
| `health`         | string  | (service sinks only) `ok`, `unreachable`, `stale-schema`, `degraded`. |

A client MAY emit multiple entries when it writes the same data to
multiple sinks (e.g. file *and* service during a migration
window).  Each entry is independent for budget accounting.

#### 17.4 Backwards compatibility with `disk_writes` (v0.5)

`disk_writes` (§3) remains a valid surface for v0.5 clients.  A
v0.6 sigmond MUST treat a client that declares only `disk_writes`
as if it had declared the equivalent `data_sinks`:

```
# Auto-promotion (logical):
data_sinks = data_sinks_from_inventory or [
    {"kind": "file",
     "target": dw.path,
     "schema_ref": null,
     "retention_days": dw.retention_days,
     "mb_per_day": dw.mb_per_day}
    for dw in disk_writes
]
```

A v0.6 client SHOULD declare `data_sinks` directly when it ships any
non-file sink.  A v0.6 client MAY continue to publish `disk_writes`
alongside `data_sinks` for tools that read either; if both are
present and disagree, sigmond logs a `warn`-level issue and prefers
`data_sinks`.

#### 17.5 Obligations for `service` sinks

A client that declares a `service` sink MUST:

1. **Read connection facts from `coordination.env`.**  The backend
   URL, credentials, and per-mode database aliases are published
   as environment variables in `coordination.env`.  Direct reads
   of `coordination.toml` are not required and not recommended.
2. **Treat backend unavailability as non-fatal.**  When the
   backend is unreachable, the client MUST continue running.  How
   a client handles in-flight rows is its own choice (queue to
   file sidecar, drop with metric, refuse new work) but MUST be
   reflected in the entry's `health` field.  Silent loss is a
   contract violation.
3. **Validate `schema_ref` at startup.**  The client MUST query
   the live backend for the `<database>.<table>` column hash and
   compare against the `schema_version` it was built with.
   Mismatch sets `health = "stale-schema"` and a `warn`-level
   `validate --json` issue.
4. **Stay no-op when no service backend is configured.**  When the
   `coordination.env` connection facts are absent, the service
   writer MUST be a silent no-op.  Standalone deployments (no
   sigmond) MUST still run; the client falls back to file-only.
5. **Ship a schema migration directory.**  The client repo
   contains a directory of numbered idempotent SQL files
   (`001_*.sql`, `002_*.sql`).  Sigmond's `smd apply` runs them in
   order at install/upgrade.  The relevant block in `deploy.toml`
   names the schema directory and the version the client expects.

A client MAY use a writer-side helper such as
`sigmond.hamsci_sink.Writer` to satisfy items 1–4; nothing in §17
mandates a particular library.

A client MAY also reference a schema it does not itself own — one
whose DDL is vendored from an external source — by citing the
upstream-pinned schema version in `schema_ref` rather than shipping
its own migrations.  In that case the client does not own the
migration directory; it only declares the version it expects.

#### 17.6 Sigmond's view

Sigmond uses `data_sinks` for:

- **Disk budget accounting.**  Sums `mb_per_day` across all sinks,
  grouped by physical filesystem (for file sinks) and by service
  backend (for service sinks); compares against `[disk_budget]`
  thresholds.
- **`smd diag` annotations.**  Surfaces `health = "unreachable"`
  or `"stale-schema"` for service sinks; surfaces missing-path or
  permission-denied for file sinks.
- **Status output.**  `smd status <client>` may list one row per
  sink.

Sigmond MUST treat unknown `kind` values as opaque — it accepts
them (forwards-compatibility for future sink kinds), counts them
in `mb_per_day`, but does not enforce kind-specific obligations.
Sigmond MUST NOT enforce that any particular client opt into
`service` sinks; the file path is always sufficient for v0.6
conformance.

### 18. Timing authority and the RTP-default fallback (v0.7)

#### 18.1 Scope

Two orthogonal axes motivate this section.

**Axis 1 — what the client needs from UTC:**

- **Sample-labeling clients** — want a UTC label for each sample
  they record (post-hoc analysis, archival, spot timestamps).
  Tolerant of latency; benefits from authority correction but does
  not need it at decision time.
- **Hard-deadline clients** — must *act* at a target UTC moment
  (start a recording at chirp launch, stop at a scheduled boundary,
  trigger an instrument cycle).  Need a current best estimate of
  UTC to convert their target into a target sample on their own
  substrate; the quality of that estimate sets the timing budget
  for the captured phenomenon.

**Axis 2 — what substrate the client operates on:**

- **Radiod-substrate clients** (`data_path.kind ∈ {radiod-ka9q-python,
  radiod-direct}`) — receive RTP samples from a radiod.  Their
  "ruler" is the RTP sample counter of the radiod stream they
  consume.  The authority annotates *that* substrate.
- **Non-radiod clients** (`data_path.kind ∈ {kiwisdr, file, other}`,
  including data-source clients like `mag-recorder` and the
  KiwiSDR-based recorder migrating from `wsprdaemon` v3) — have
  their own native substrate (a magnetometer's sample clock, a
  KiwiSDR's time-tag, a file's recorded wall-clock).  They still
  want the best available UTC, both for sample-labeling and for
  hard-deadline scheduling against their own substrate.  The
  authority annotates *time itself* for them, not any radiod
  stream — typically via the host's monotonic clock as a bridge.

Both axes compose to four cases, all addressed below.  Per
[`ARCHITECTURE-FIRST-PRINCIPLES.md`](https://github.com/mijahauan/hf-timestd/blob/main/docs/ARCHITECTURE-FIRST-PRINCIPLES.md)
the *substrate* (radiod RTP, magnetometer sample-clock, KiwiSDR
time-tag, host monotonic) is whatever the client's data plane
provides; the *annotation* is the UTC mapping a timing authority
publishes onto that substrate.

This section names what a *consuming client* may rely on without
specifying the wire protocol.  The producer-side reference for what
a timing authority *is* lives in
[`hf-timestd/docs/ARCHITECTURE-FIRST-PRINCIPLES.md`](https://github.com/mijahauan/hf-timestd/blob/main/docs/ARCHITECTURE-FIRST-PRINCIPLES.md).

#### 18.2 Two modes

- **RTP-default mode.**  The client uses the RTP-to-UTC mapping as
  published by radiod (its anchor and the nominal sample rate).
  No dependency on a timing-authority peer.  Quality is bounded by
  whatever governs the radiod machine's clock (wall, WAN NTP,
  chrony, GPSDO).  Standalone-safe and always the fallback.
- **Authority-corrected mode.**  The client subscribes to a timing
  authority (hf-timestd is the reference implementation) and uses
  the authority's published snapshot for its sample↔UTC math.
  Optional.  Per
  [`ARCHITECTURE-FIRST-PRINCIPLES.md`](https://github.com/mijahauan/hf-timestd/blob/main/docs/ARCHITECTURE-FIRST-PRINCIPLES.md)
  §2, the tier system ranks authority quality (T0 worst, T5 best,
  T6 cross-check).

The two modes are not mutually exclusive across a station: one
client may operate in RTP-default while a peer operates in
authority-corrected.  Sigmond reports the choice; it does not
enforce it.

#### 18.3 Discovery

A timing authority is a property of the *substrate* a subscriber
operates on, not of any particular host.  hf-timestd may be
co-located with the radiod, with the consumer client, or on a
third host — the published annotation is correct for any
subscriber of the same substrate.

Two scopes of authority pointer exist.  Both are published in
`coordination.env`; a client reads whichever applies to its data
plane.

**Per-radiod scope (for radiod-substrate clients).**  Parallel to
§8.  Names the authority that annotates a specific radiod's RTP
stream:

```
RADIOD_BEE3_RX888_TIMING_AUTHORITY=hf-timestd@bee3
RADIOD_BEE3_RX888_TIMING_AUTHORITY_ENDPOINT=unix:///run/hf-timestd/authority.sock
RADIOD_BEE3_RX888_TIMING_AUTHORITY_TIER_MIN=T4
```

A radiod-substrate client MAY read these on startup and on SIGHUP
and subscribe.  Absence of `RADIOD_<id>_TIMING_AUTHORITY*` keys =
RTP-default mode for streams from that radiod.

**Station-wide scope (for non-radiod clients, and as a fallback
for radiod clients).**  Names the station's best available timing
authority, independent of any radiod stream:

```
TIMING_AUTHORITY=hf-timestd@bee3
TIMING_AUTHORITY_ENDPOINT=unix:///run/hf-timestd/authority.sock
TIMING_AUTHORITY_TIER_MIN=T4
```

A non-radiod client (`data_path.kind ∈ {kiwisdr, file, other}`)
MAY read these on startup and on SIGHUP and subscribe.  Absence of
station-wide `TIMING_AUTHORITY*` keys = host-clock-default mode
(the non-radiod analogue of RTP-default: the client uses whatever
native UTC its data source or host clock provides, with no
authority correction).

**Precedence for radiod-substrate clients.**  A radiod-substrate
client MAY also use the station-wide keys as a fallback when its
per-radiod keys are absent.  Per-radiod takes precedence when both
are present.  Sigmond MAY populate both pointing at the same
authority — the per-radiod variant just identifies which RTP
stream the authority is annotating.

**Standalone discovery.**  Any client MAY accept an authority
endpoint in its own config (TOML).  A client MUST fall back to its
mode-appropriate default (RTP-default for radiod clients,
host-clock-default for non-radiod clients) if no endpoint is
configured *or* the configured endpoint is unreachable.
Reachability is checked at startup and on SIGHUP; transient
unreachability during steady-state operation is handled per §18.5
hard-deadline gating.

**Key format.**  Per-radiod keys: `RADIOD_<id>_TIMING_AUTHORITY`,
`RADIOD_<id>_TIMING_AUTHORITY_ENDPOINT`,
`RADIOD_<id>_TIMING_AUTHORITY_TIER_MIN`.  Station-wide keys:
`TIMING_AUTHORITY`, `TIMING_AUTHORITY_ENDPOINT`,
`TIMING_AUTHORITY_TIER_MIN`.  Endpoints are URIs (`unix://`,
`tcp://`, etc.).  `TIER_MIN` is the operator's floor for authority
acceptance; the client's own threshold under §18.5 MAY be stricter
but MUST NOT be looser.  Sigmond owns the `coordination.env`
write path; clients read only.

#### 18.4 What the authority publishes

A subscribing client gets a periodic snapshot containing at
minimum:

| Field                       | Meaning                                                                 | Used by                       |
|-----------------------------|-------------------------------------------------------------------------|-------------------------------|
| `utc_anchor_ns`             | The UTC time corresponding to the anchor moment, in ns since the epoch. | All subscribers.              |
| `tier`                      | Authority tier from [`ARCHITECTURE-FIRST-PRINCIPLES.md`](https://github.com/mijahauan/hf-timestd/blob/main/docs/ARCHITECTURE-FIRST-PRINCIPLES.md) §2. | All subscribers.              |
| `sigma_ns`                  | 1-σ uncertainty of `utc_anchor_ns` at the time of measurement.          | All subscribers.              |
| `snapshot_age_s`            | Wall time since the last successful authority observation.              | All subscribers.              |
| `host_monotonic_at_anchor`  | Authority host's `CLOCK_MONOTONIC_RAW` (ns) at the anchor moment.       | Non-radiod subscribers.       |
| `rtp_anchor_sample`         | An RTP sample number on the named stream.                               | Radiod-substrate subscribers. |
| `rate_samples_per_utc_sec`  | Measured sample rate (not nominal); used for forward projection.        | Radiod-substrate subscribers. |
| `radiod_id`                 | Which RTP stream this snapshot is the offset for.                       | Radiod-substrate subscribers. |

The radiod-specific fields (`rtp_anchor_sample`,
`rate_samples_per_utc_sec`, `radiod_id`) are present when the
subscriber requested a per-radiod authority via §18.3 per-radiod
keys.  The non-radiod field (`host_monotonic_at_anchor`) is present
when the subscriber requested a station-wide authority via §18.3
station-wide keys.  A subscriber MUST use only the fields
appropriate to its subscription type; the producer MAY include
both sets in a single snapshot (e.g. when serving co-located
radiod and non-radiod clients from the same authority instance).

Wire format and transport are out of scope here; the contract
names the fields a client may rely on.

#### 18.5 Client obligations

Two operating modes; for the authority-corrected mode the
substrate-conversion formula differs by subscriber type
(radiod-substrate vs. non-radiod).

**Default mode (RTP-default for radiod clients; host-clock-default
for non-radiod clients).**  A client operating in default mode MUST:

- Convert its native samples to UTC using whatever its data source
  provides natively (radiod's published anchor + nominal rate for
  radiod clients; the data source's own time-tag or the host
  clock for non-radiod clients).
- Report `timing_authority_applied: null` in `inventory --json`
  (§3).
- Report `uses_timing_calibration` accurately: `false` if the
  client never subscribes to an authority; `true` if it would
  subscribe were one available (the value describes capability,
  not the currently-active mode — the current mode is reported by
  `timing_authority_applied`).

**Authority-corrected mode — common obligations.**  Any client
operating in authority-corrected mode MUST:

- Re-fetch the authority snapshot at a cadence appropriate to its
  tolerance.  Recommended: at least once per scheduled action.
  Minimum: at startup and on SIGHUP.
- Report the currently applied authority in `inventory --json` via
  the `timing_authority_applied` field (§3 amendment).

**Authority-corrected mode — radiod-substrate subscribers.**
Compute UTC as:

```
utc(rtp_n) = utc_anchor_ns
           + (rtp_n − rtp_anchor_sample) × 1e9 / rate_samples_per_utc_sec
```

Then apply the §8 chain-delay correction (if any) *after* the §18
conversion.  See §18.6.

**Authority-corrected mode — non-radiod subscribers.**  Compute
UTC by bridging through the host's monotonic clock:

```
utc(t_local) = utc_anchor_ns
             + (host_monotonic_now − host_monotonic_at_anchor)
```

`host_monotonic_now` is the subscribing client's own
`CLOCK_MONOTONIC_RAW` reading at the moment it wants UTC.  This
formulation is exact when subscriber and authority are co-located
(shared kernel monotonic clock).  When they are on different
hosts, the wire protocol layer (hf-timestd's own
interface concern) is responsible for bridging the two hosts'
monotonic clocks; the additional uncertainty added by that bridge
is reflected in the snapshot's `sigma_ns` field.

§8 chain-delay does not apply to non-radiod clients (it is a
property of the radiod analog/ADC pipeline).

**Hard-deadline start/stop decisions.**  A client that makes hard
start/stop decisions in authority-corrected mode MUST additionally:

- Gate the decision on
  (`tier ≥ configured_min_tier`) AND
  (`snapshot_age_s ≤ configured_max_age`) AND
  (`sigma_ns ≤ configured_max_sigma`).
- When the gate fails, the client MUST NOT silently fall back to a
  worse estimate.  It MUST either (a) refuse the action and log
  the refusal as a first-class event with the failing budget, or
  (b) downgrade to its default mode with the downgrade also logged
  as a first-class event.  Silent degradation is a contract
  violation.
- Report the configured thresholds and the most recent gate result
  in `/status` per §13.4 so sigmond's TUI can surface "timing
  budget OK / breached" without polling the client.

**Annotation propagation.**  A client MUST NOT propagate
authority-corrected timestamps to downstream consumers (sinks,
spots, archives, peer clients) without also recording the tier and
σ that produced them.  This preserves
[`ARCHITECTURE-FIRST-PRINCIPLES.md`](https://github.com/mijahauan/hf-timestd/blob/main/docs/ARCHITECTURE-FIRST-PRINCIPLES.md)
§3: the annotation travels with the sample, regardless of
substrate.

#### 18.6 Relationship to §8

§8 (chain-delay) and §18 (timing-authority) compose.  §8 is a
*static* hardware-level correction (fixed ns offset per radiod
analog/ADC path); §18 is a *dynamic* timeline-anchor correction
(epoch + rate refreshed per authority cycle).  Applied together:

```
utc_final_ns = utc_via_§18(rtp_sample_n) − chain_delay_ns_§8
```

§18 corrects the radiod host clock's contribution to RTP→UTC; §8
corrects the analog-front-end → ADC pipeline delay.  They address
different errors and never replace each other.  A client may use
both, neither, or just one — the four combinations are all legal
(and all reported honestly in inventory).

#### 18.7 Sigmond's view

Sigmond:

- Surfaces `timing_authority_applied` per client in `smd status`
  and `smd diag`.
- Cross-references peer clients of the same radiod: if one peer is
  authority-corrected and another is RTP-default, that's a
  degraded but legal state; sigmond reports it for operator
  awareness rather than failing validation.
- Flags clients reporting `tier ≤ T1` or `snapshot_age_s` above
  their configured threshold.
- Does not require any client to operate in any particular mode.
  RTP-default is always conformant.

#### 18.8 What this section does NOT do

- It does not define the wire protocol between hf-timestd and a
  subscriber — that is hf-timestd's interface concern, owned by
  its own `INTERFACE.md` (or equivalent) rather than this
  contract.
- It does not require any client to be hf-timestd-aware.
  RTP-default mode is always available and always conformant.
- It does not deprecate §8.  Chain-delay remains a distinct,
  necessary correction.

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
  §10, §11 — see §9 retrofit checklist above).
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
  items, bundled with the v0.3 retrofit work).
- `psk-recorder` was built greenfield against v0.3; v0.4 additions
  are a minor-release retrofit, not a fresh rewrite.
- **v0.4 → v0.5** adds §5.0–§5.5 (lifecycle scope and unit
  declaration clarifications), §13 (control surface), §14
  (configuration interview), §15 (radiod channel contributions),
  and §16 (independent data-source clients).  Amendments: §3 adds
  `control_socket` and `deploy_toml_path` per instance; §6 relaxes
  the `ka9q-python` MUST to SHOULD with §16 as the explicit opt-out
  path.  v0.4 clients pass validation on a v0.5 sigmond unchanged;
  sigmond treats a missing `data_path` as
  `kind = "radiod-ka9q-python"` (§16.3) and treats a missing
  `control_socket` or `deploy_toml_path` as "use the §13.1 / §12.5
  canonical default."  Inventory mismatch (v0.4 client on v0.5
  sigmond) emits a warn-level issue, not a hard fail.
- **Clients requiring v0.5 retrofit:** all conformant clients
  retrofitted on 2026-05-04 — `hf-timestd`, `psk-recorder`,
  `wspr-recorder` declare `data_path = {kind:
  "radiod-ka9q-python", radiod_id: ...}`.  No client has yet
  implemented §13's control socket (advisory `control_socket`
  paths are published; the server itself ships in subsequent
  releases per the §13.1 transport spec).
- **v0.5 → v0.6** adds §17 (engine-agnostic output sinks).  v0.5
  clients pass validation on a v0.6 sigmond unchanged; sigmond
  auto-promotes a client's `disk_writes` array (§3) into the
  equivalent file-only `data_sinks` shape per §17.4.  Only clients
  that opt into `service` sinks need to declare `data_sinks`
  explicitly and bump `contract_version` to `0.6`.  §17 was
  subsequently revised in place to make the `kind` enum
  engine-agnostic: a sink is either a local `file` or an external
  `service`, with no database product named in the contract.
- **v0.6 → v0.7** adds §18 (timing authority and the default
  fallback), gives the previously-undefined v0.2 booleans
  `uses_timing_calibration` and `provides_timing_calibration`
  their contract semantics, adds `timing_authority_applied` per
  instance (§3), and amends §16.5 so non-radiod clients (e.g.
  `mag-recorder`, KiwiSDR-based recorders) can also subscribe to
  a timing authority via the station-wide discovery path.  v0.6
  clients pass validation on a v0.7 sigmond unchanged; sigmond
  treats a missing `timing_authority_applied` as `null` (§18
  default mode, always conformant) and treats missing or unset
  `uses_timing_calibration` / `provides_timing_calibration` as
  `false`.  No client is required to subscribe to a timing
  authority; the §18 retrofit is opt-in per client.  Inventory
  mismatch (v0.6 client on v0.7 sigmond) emits an informational
  note, not a warn-level issue.
- **Clients requiring v0.7 retrofit (opt-in):**
  - `hf-timestd` — already `provides_timing_calibration: true`;
    needs to publish both per-radiod (§18.3 per-radiod keys) and
    station-wide (§18.3 station-wide keys) entries via sigmond's
    write path, expose the §18.4 snapshot fields to both
    radiod-substrate and non-radiod subscribers (including
    `host_monotonic_at_anchor` for the latter), and document its
    endpoint URI.  No behaviour change for clients that don't
    subscribe.
  - Any radiod-substrate client that wants to subscribe
    (start/stop scheduling, sub-ms labelling on a poorly-disciplined
    radiod host) — reads the §18.3 per-radiod keys, fetches
    snapshots per §18.4, reports `timing_authority_applied` per
    §3, and (if hard-deadline) implements §18.5 gating.
  - Any non-radiod client that wants to subscribe (e.g.
    `mag-recorder` for hardware-timed magnetometer samples,
    KiwiSDR-based recorders for audio capture from an external
    SDR) — reads the §18.3 station-wide keys, fetches snapshots
    per §18.4, bridges via `host_monotonic_at_anchor` per §18.5,
    and reports `timing_authority_applied` per §16.5 (omitting
    `radiod_id`).
  - All other clients remain in default mode (RTP-default for
    radiod, host-clock-default for non-radiod) and are conformant
    unchanged.
