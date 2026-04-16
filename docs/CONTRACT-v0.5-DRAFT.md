# HamSCI Client Contract — v0.5 Draft (Control Surface)

**Status:** DRAFT for review by Rob (AI6VN) and Michael (AC0G). Not yet
folded into `CLIENT-CONTRACT.md`. Targets a §13 addition, §5 lifecycle
clarifications + new §5.1–§5.5, and enhanced §3 amendment (`inventory
--json`).

**Motivation**

Contract v0.4 standardized how clients are configured, deployed, and
discovered. It does not yet standardize how a *running* client reports
its live state or accepts lightweight runtime control. Today each
client invents its own answer (hf-timestd has a web API; psk-recorder
has none; wspr-recorder relies on the spool dir as an indirect health
signal). That divergence blocks two things we now want:

1. **A reusable Sigmond TUI / `smd status`** that renders any
   contract-conformant client without bespoke code per client.
2. **Cross-client coordination**, where Sigmond detects situations
   inside one client that affect peers (multicast group collisions,
   spool-volume contention, IGMP-snooping failures, decoder
   back-pressure starving a shared CPU budget).

v0.5 adds a narrow, mandatory **control surface** that exposes only
inter-client / interface-level facts. Deep per-client debugging stays
in each client's own surface (e.g. hf-timestd's web API). Sigmond
remains the coordinator, not a god-object that mirrors every client's
internals.

---

## §5. Lifecycle scope and systemd unit declaration (v0.5, CLARIFIED + NEW)

### 5.0 Declaring units in `deploy.toml`

Every client's `deploy.toml` (§5 in CLIENT-CONTRACT.md) declares which
systemd units sigmond may start, stop, restart, reload, or monitor.
This section clarifies the unit types and introduces templated-unit
support for multi-instance clients.

#### 5.0.1 Unit kinds

```toml
[systemd]
units           = ["foo.service", "foo-daily.timer", "foo.target"]      # concrete names
templated_units = ["foo@.service", "foo-index@.timer"]                  # templates
```

- **`units`** — concrete unit names (services, timers, targets).
  Sigmond will start/stop these by their literal names.
- **`templated_units`** — template names (containing `@`) that sigmond
  will instantiate per discovered instance (see §5.1).
- Either key MAY be absent (treated as empty).

#### 5.0.2 Backward compatibility

A templated name (contains `@.service` / `@.timer` / `@.target`)
appearing in `units` is deprecated but accepted for v0.4 clients
already deployed on production (e.g. `psk-recorder@.service` in v0.4
releases). Sigmond will:

1. Detect the `@` marker internally and normalize it into `templated_units`
   with a deprecation warning.
2. Continue operating correctly.
3. NOT break existing deployments of psk-recorder or wspr-recorder on bee3
   until those clients explicitly migrate their `deploy.toml` files.

**Guidance for new clients:** Use the `templated_units` key.

### 5.1 Instance enumeration for templated units

When a client's `deploy.toml` declares `templated_units` (e.g.
`psk-recorder@.service`), sigmond discovers live instances and expands
the template for each one. This is the mechanism allowing multi-instance
clients without repeated entries in `coordination.toml`.

#### 5.1.1 Configured vs. known instances

For each template (e.g., `psk-recorder@.service`):

- **Configured** = `{instance | /etc/<client-name>/env/<instance>.env
  exists}`. This is the **authoritative** set that sigmond will operate on
  for lifecycle verbs (start, stop, restart, reload). The env file is the
  §4 configuration convention already used by all three reference clients.
- **Known** = configured ∪ {instance | `systemctl list-units
  '<template>@*.service' --all` reports it}. Known instances include orphans
  (instances running but with no env file — typically leftover from a removed
  instance).

#### 5.1.2 Lifecycle verb scope

- **`smd start / stop / restart / reload <component>`** (without explicit
  instance) operate on all **configured** instances of `<component>`.
- **`smd list / status <component>`** report on **known** instances,
  flagging any in (known − configured) as **orphaned**. Orphans are
  running but absent from configuration — a signal of drift that an
  operator should investigate and clean up.

#### 5.1.3 Env file convention

All multi-instance clients MUST use:

```
/etc/<client-name>/env/<instance>.env
```

The `<instance>` part in the path is matched against the systemd template
instantiation. For example, if a client has instances `default`, `lf`, and
`backup`, the paths are:

```
/etc/psk-recorder/env/default.env
/etc/psk-recorder/env/lf.env
/etc/psk-recorder/env/backup.env
```

The units would be instantiated as:

```
psk-recorder@default.service
psk-recorder@lf.service
psk-recorder@backup.service
```

### 5.2 Lifecycle scope boundary

Sigmond's lifecycle verbs act **only** on the resolved union of:

- Concrete `units` (as-named).
- Instance-expanded `templated_units`.

**Out of scope:** Auxiliary units a client ships but omits from the
arrays. Examples:

- Timers or failure handlers not in `units`.
- Socket units.
- Oneshot units used for setup / teardown.

Clients manage these via systemd `WantedBy=` / `PartOf=` relationships
on lifecycle-managed units, or via their own setup oneshot.

**Exception — targets with children:**

If `units` declares a target (e.g., `timestd-metrology.target`), sigmond
will expand it via `systemctl list-dependencies --reverse <target>` before
`stop`, because `systemctl stop <target>` does not stop `Wants=` children.

- Clients whose target contains lifecycle-managed children **MUST** use
  `PartOf=<target>` on those children so stop propagates correctly.
- Targets themselves are brought down last (after all `PartOf=` units).

### 5.3 The `reload` verb and `ExecReload` convention

Clients MAY declare `ExecReload=/bin/kill -HUP $MAINPID` (or equivalent)
in their unit files to support the `smd reload <component>` verb.

- `smd reload` maps to `systemctl reload <unit>` when `ExecReload` is present.
- Falls back to `systemctl try-restart` otherwise (reload-or-restart).
- This is **distinct from** v0.5's `/reload` control-socket endpoint (§13.3):
  - `smd reload` = OS-level signal to the unit (works without control
    socket; needed for v0.5's log-level changes, §11).
  - `POST /reload` on control socket = in-process config re-read with
    structured response (which keys applied vs require restart).

**Optional auto-routing flag:**

`smd reload --via=auto|systemd|socket` (default: `auto`) prefers the
control socket when `inventory --json` reports a `control_socket` path
(see §3 amendment), falling back to systemd if absent.

### 5.4 Start / stop ordering

- **Start order:** Components are started in the order they appear in
  `/etc/sigmond/coordination.toml`'s `[[clients.<name>]]` lists, with
  `radiod` always first.
- **Stop order:** Reverse of start order.

**Cross-client dependencies:** Sigmond owns station-level ordering.
Clients MUST NOT declare cross-client `After=` / `Requires=` dependencies
beyond the implicit "radiod is upstream." Operator mistakes here (e.g.,
`psk-recorder` depends on `wspr-recorder`) create fragile configurations
that sigmond cannot resolve. Sigmond will validate `coordination.toml`
and warn if suspicious cross-client unit links are detected.

### 5.5 Lifecycle lock and atomicity

Every mutating verb (`install`, `apply`, `start`, `stop`, `restart`,
`reload`, `update`) acquires an flock on:

```
/var/lib/sigmond/lifecycle.lock
```

This prevents concurrent apply-vs-restart races. `list` and `status` are
lock-free readers.

---

## §13. Control surface (v0.5, NEW)

### 13.1 Transport

- Each running client MUST expose an HTTP/JSON endpoint over a
  **unix-domain socket** at:

  ```
  /run/<client-name>/control.sock                  # single-instance
  /run/<client-name>/<instance>.control.sock       # multi-instance
  ```

- Socket is created mode `0660`, owned by the client's service user,
  group `sigmond` (created by sigmond install). Sigmond and the
  client's own operator can read; nobody else.

- Implementation in stdlib only: `http.server.BaseHTTPRequestHandler`
  bound to a `socketserver.UnixStreamServer`. No third-party web
  framework dependency for conformance.

- `curl --unix-socket /run/<client>/control.sock http://./status`
  MUST work for headless debugging. This is the headless-first
  equivalent of opening a TUI panel and is the property that keeps
  the contract debuggable from SSH.

**Why unix sockets, not TCP / MQTT:** keeps v0.5 single-host, no
broker dependency, no auth/TLS in scope, no port collisions on hosts
running multiple radiod + multiple clients. The socket path is the
identity. Multi-host aggregation (LAN, possibly via SSH tunnel or an
optional MQTT bridge sidecar) is a v0.6+ concern; the schema below is
designed to survive that promotion unchanged.

### 13.2 Mandatory endpoints

| Method | Path        | Purpose |
|--------|-------------|---------|
| GET    | `/healthz`  | Liveness. 200 if process is up and event loop responsive. |
| GET    | `/readyz`   | Readiness. 200 only if input is flowing AND output path (spool / upload / etc.) is writable. |
| GET    | `/status`   | One-shot snapshot. Schema in §13.4. |
| GET    | `/metrics`  | Prometheus text format. Counters and gauges from §13.4 plus client-specific extras. |

Mandatory endpoints MUST respond in <100 ms under nominal load and
MUST NOT block on I/O against radiod, the network, or downstream
consumers — they read cached state updated by the client's own loop.

### 13.3 Optional endpoints (recommended where applicable)

| Method | Path                            | Purpose |
|--------|---------------------------------|---------|
| GET    | `/channels`                     | List per-channel state (FT8 channels, WSPR bands, hf-timestd outputs). |
| GET    | `/channels/{id}`                | Per-channel detail. |
| POST   | `/channels/{id}/enable`         | Runtime enable, no config edit. |
| POST   | `/channels/{id}/disable`        | Runtime disable. |
| GET    | `/events?since=<seq>&limit=<n>` | Ring buffer of structured events (decode, drop, upload-fail, IGMP-rejoin). |
| POST   | `/reload`                       | Re-read config. Body: `{"dry_run": bool}`. Response lists keys applied vs keys requiring restart. |

Clients MAY add further endpoints under `/x/<client-name>/...` for
client-specific debug. Sigmond never depends on `/x/...`.

### 13.4 `/status` JSON schema

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

**Field rules:**

- `state = degraded` MUST be set if any of: no input packets in
  >2× expected interval, spool not writable, pipeline backpressure
  asserted, or downstream `last_success_age_s` exceeds a
  client-defined threshold.
- All `*_age_s` fields are seconds since the named event. Avoids
  client/server clock-skew issues that absolute timestamps cause.
- A field that does not apply to a given client MAY be omitted
  entirely (e.g. hf-timestd has no `spool`; wspr-recorder has no
  per-channel `decodes_15m`, it has `spots_15m`).
- The schema is **additive**: clients MAY include extra keys; sigmond
  and TUI widgets MUST ignore unknown keys.

### 13.5 Mapping to existing clients

**`psk-recorder`** — already exposes most of this internally; needs
the socket server and JSON marshalling. `pipeline` here is the
decoder (`jt9`/`wsjtx`) lag and PSKReporter upload queue.

**`wspr-recorder`** — `channels` becomes the band list,
`decodes_15m` becomes `spots_15m`, `pipeline` is the
wsprdaemon-client handoff (spool depth IS the queue, so
`queue_depth = spool.depth_files`; `last_success_age_s` is the
newest deletion from spool, observed by inotify or stat).

**`hf-timestd`** — keeps its existing web API. v0.5 adds the unix
socket as a *parallel* surface that reports only the inter-client
slice (multicast groups, output writable, BPSK PPS calibration
status). The web API remains authoritative for the science and
for deep debug. Sigmond reads only the unix socket.

### 13.6 Inter-client effects sigmond can detect from §13.4 alone

This is the payoff — these are concrete cross-client conditions
sigmond can flag without reaching into any client's internals:

1. **Multicast group collision** — two clients on the same host
   reporting the same `multicast.groups_joined[].group` when they
   should not.
2. **IGMP-snooping silent failure** — `last_pkt_age_s` is small but
   `last_igmp_report_age_s` is climbing past the switch's query
   interval; classic ka9q-radio gotcha.
3. **Shared-spool exhaustion** — multiple clients writing to the same
   `spool.fs`, aggregate `bytes_written_1m` rising, `oldest_age_s`
   on a downstream consumer climbing.
4. **CPU budget breach** — sum of `resources.cpu_pct_1m` across
   clients on one host exceeds the budget sigmond allocated.
5. **Radiod loss** — multiple clients on the same `radiod.id` all
   reporting `last_status_rx_age_s` climbing in lockstep ⇒ radiod is
   the fault, not any one client.
6. **Back-pressure cascade** — one client's `pipeline.backpressure
   = true` correlated with peer's `multicast.drops_1m` rising.

None of these require sigmond to know what any client *does*
internally — only what it exposes at the boundary.

---

## §3 amendment (expanded)

`<client> inventory --json` adds two new fields per instance:

```json
{
  "instance": "default",
  "control_socket": "/run/psk-recorder/control.sock",
  "deploy_toml_path": "/opt/git/psk-recorder/deploy.toml",
  ...
}
```

- **`control_socket`** (v0.5): Path where sigmond can find this instance's
  control-socket endpoint (see §13.1). Sigmond uses this for discovery so
  the socket path convention in §13.1 is a default, not a hardcoded
  assumption. If the field is absent, sigmond falls back to the path
  derived from the convention.
- **`deploy_toml_path`** (v0.5): Path to the client's `deploy.toml` file.
  Sigmond discovers each client's lifecycle declarations (§5) via this path
  rather than inventing a second discovery mechanism. If absent, sigmond
  falls back to `/opt/git/<client-name>/deploy.toml` (the Pattern A
  canonical location, §12.5 in CLIENT-CONTRACT.md).

---

## What this is NOT (scope discipline)

- **Not a config API.** `/reload` re-reads the on-disk config file
  written by the operator (or by sigmond drop-in for
  `coordination.env`). It does not accept config payloads. Config
  authoring stays out of the runtime surface.
- **Not multi-host.** Single-host unix socket only. Multi-host is
  v0.6+ (probably an opt-in MQTT bridge or SSH-tunnel aggregator
  that consumes `/status` and republishes).
- **Not a debug API.** Per-client deep state stays in the client's
  own surface. Sigmond reads only the boundary.
- **Not authenticated.** Filesystem permissions on the socket are the
  authn boundary. If the surface ever leaves the host, the bridge
  layer owns auth, not the contract.

---

## Open questions for Rob

1. Is the unix-socket-only stance acceptable for v0.5, or do you
   want a TCP fallback for clients running under non-systemd
   supervision?
2. For `wspr-recorder` specifically, does `pipeline` cleanly model
   the wsprdaemon-client handoff, or would you rather split it into
   `producer` / `consumer` halves so the seam between
   `wspr-recorder` and `wsprdaemon-client` is explicit in `/status`?
3. `/reload` semantics: should the response enumerate which keys
   actually changed (diff against last load), or just which keys are
   hot vs. restart-required (capability list)? The latter is simpler
   to implement and probably enough for sigmond.
4. Are there other inter-client effects you've hit in wsprdaemon
   operations that §13.6 should call out? IGMP and shared-spool are
   the ones I've personally been bitten by; you've run far more
   stations.
5. Versioning: bump contract minor (v0.5) since this is purely
   additive — old clients remain conformant at v0.4 and sigmond
   degrades gracefully when `/status` is absent. Agree?

---

## Implementation phasing (proposed)

- **Phase A** — psk-recorder reference implementation of §13
  (mandatory endpoints + `/channels`). Smallest surface, already
  has the internal state shape closest to the schema. Validates the
  socket-server skeleton that becomes a shared `clientlib`.
- **Phase B** — wspr-recorder retrofit using the same skeleton.
  Drives the producer/consumer question in OQ #2.
- **Phase C** — sigmond ships `smd status` consuming both, plus the
  `sigmond-tui` widget library (`ServiceCard`, `ChannelTable`,
  `SpoolGauge`, `EventLog`, `MulticastHealth`).
- **Phase D** — hf-timestd adds the parallel unix-socket surface
  (web API stays). Independent planning per the existing project
  notes — its surface is more involved.
- **Phase E** — fold v0.5 into `CLIENT-CONTRACT.md` proper, retire
  this draft.
