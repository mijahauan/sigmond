# sigmond CLI v2 — verb surface spec

Locks the verb shape, cluster boundaries, and alias-keep decisions for
the next major rework of `bin/smd`. Companion to
[TUI-FUNCTION-INVENTORY.md](TUI-FUNCTION-INVENTORY.md): the inventory
catalogues today's surface; this doc decides what tomorrow's surface
looks like and why.

The mechanical migration (parser rewiring, deprecation warnings,
docs/completion regen) is a follow-up. This spec is the contract the
migration commit must satisfy.

Source state: commit `01d6bb7` (2026-05-24).

---

## 1. Why a v2

Today's 33-verb surface evolved organically. The inventory pulled the
shape apart and found that almost every verb maps cleanly to a single
operator persona — operator / installer / debugger — but the CLI
itself doesn't teach that structure. Three frictions show up:

1. **Bare verbs that hide their object.** `list` lists *what*?
   `install` installs *what*? The operator learns by reading
   `--help`, not from the verb.
2. **Redundant entries.** Five legacy `*-watch` verbs duplicate
   `watch <target>`. `software install` rewrites to top-level
   `install`. `cpu` shortcuts `diag cpu-freq`. None of these
   carry weight; they dilute `--help` and the bash-completion
   menu.
3. **Inconsistent namespacing.** `timestd-tune-storage` is a
   three-token bare verb that should sit under `storage` alongside
   `storage trim` and `storage migrate-to-sqlite`. `config` is
   well-namespaced; catalog operations (`add`/`remove`/`enable`/
   `disable`/`install`/`list`) are scattered across the top level
   despite operating on the same thing.

The TUI reorganization (§4 of the inventory) already commits to the
four-way persona model in the navigation tree. The CLI is the surface
that teaches the model first — it has to follow.

---

## 2. Persona model

Three personas drive the clustering. The same person may wear all
three hats, but each invocation lives in exactly one column:

| Persona | Cadence | Mental model |
|---|---|---|
| **Operator** | daily / weekly | "Is the host healthy? Did my edit take effect? Is this recorder still producing?" |
| **Installer** | first-time, then rarely | "Bring this host from bare metal to producing. Add a component. Migrate storage once." |
| **Debugger / Maintainer** | when something looks wrong, or when upgrading | "Why is verifier failing? What does upstream ka9q look like? What's the affinity plan vs. observed?" |

Mapping the v2 clusters to personas (a cluster's primary persona is
**bold**; secondaries listed after):

| Cluster | Primary persona | Also used by |
|---|---|---|
| Lifecycle | **Operator** | Installer (at end of bring-up) |
| Observation | **Operator** | Debugger |
| Catalog (`component`) | **Installer** | Maintainer (for `component update`) |
| Config | **Installer** (`init`/`identity`) + **Operator** (`show`/`edit`/`backup`) | — |
| Wiring (`environment` + `sources`) | **Operator** | Installer (first-time wire-up) |
| Validation | **Operator** | Debugger |
| Diagnostics (`diag`) | **Debugger** | Installer (`--apply` paths during bring-up) |
| Installation-time tuning | **Installer** | — |
| Data quality (`verifier`) | **Debugger** | Maintainer (`rehabilitate`) |
| Meta | all | — |

---

## 3. Cluster boundaries — locked

The canonical v2 verb shape, by cluster. Aliases and deprecations are
called out in §4 and §5.

### Lifecycle (bare verbs; systemctl-*shaped*)

```
start | stop | restart | reload
apply                            # reconcile runtime with declared state (not pure service control)
enable  <component>              # also reachable as `component enable`
disable <component>              # also reachable as `component disable`
```

The cluster header says "shaped," not "aligned," on purpose:

- `start / stop / restart / reload` are systemctl-aligned in both
  shape *and* semantics — they control systemd units.
- `apply` (`cmd_apply`, smd:2193) is **reconciliation**, not service
  control. It reapplies `deploy.toml` link-kind installs, reapplies
  systemd `enable`s, reconciles Proxmox `onboot`, and only cycles
  `wd-upload-*` when state actually changed. Lifecycle is the
  closest cluster — `apply` is the verb operators reach for after a
  config edit, in the same mental motion as `restart` — but it
  isn't pure lifecycle. Recorded as a tolerated approximation; see
  §6.
- `enable` / `disable` mutate the **catalog** state in
  `topology.toml` (`cmd_enable`, smd:1593; `cmd_disable` issues
  `systemctl stop` as a side-effect of the catalog toggle). They
  stay bare because the *verb shape* is what systemctl trained
  operators on, not because they operate on systemd units. They are
  *also* exposed under `component` (§3.3) so that a user who
  already typed `component ` and is reading completions discovers
  them.

### Observation (operator surfaces, primarily read-only)

```
status                           # service health snapshot
watch <wspr|psk|hfdl|codar|ka9q|uploads|verifier>
log [<client>]                   # bare = tail recent log lines (read)
log set-level [<client>] <level> # mutates coordination.env (+ SIGHUP if <client>); was: log --level
public-ip                        # external IP probe
validate                         # cross-client harmonization check
```

`log` is a small namespace: bare invocation tails recent log lines
(today's default behavior, minus the `--level` flag); `log set-level`
is the mutating sub-verb that writes the log level into
`coordination.env` and (when a client is named) sends SIGHUP. Two
argv shapes are accepted: `log set-level <level>` sets the global
`CLIENT_LOG_LEVEL` default with no SIGHUP (operator restarts /
SIGHUPs clients individually to apply); `log set-level <client>
<level>` writes a per-client override and SIGHUPs that client's
units. The split mirrors the v2 stance against mutating flags on
read verbs — same reason `list --update` became `component update`.
`log set-level` is the one Observation member that mutates; the
cluster header is "primarily" read-only for this reason.

`validate` is read-only — it reports inconsistencies across clients
(radiod / freq / CPU / disk harmonization rules). The operator's
follow-up action is to edit config and `apply`, but the verb itself
belongs with the read-only operator surfaces.

### Catalog (`component` namespace)

The unifying namespace for everything that operates on the catalog of
software components.

```
component list                   # canonical; bare `list` kept as alias (see §4)
component install [name]         # canonical; bare `install` kept as alias (see §4)
component update [--apply]       # replaces `list --update` / `list --apply`
component add <repo>             # canonical; bare `add` REMOVED (no alias)
component remove <name>          # canonical; bare `remove` REMOVED (no alias)
component enable  <name>         # also reachable as bare `enable`
component disable <name>         # also reachable as bare `disable`
```

Rationale for the asymmetric alias choice (`list`/`install` aliased,
`add`/`remove` not): `list` and `install` are operator-facing and
hit often during routine catalog browsing — preserving the bare form
avoids burning operator muscle memory. `add`/`remove` are
installer-facing, rare, and easily confused with `apt add`/`apt
remove` package management (they're really *clone* and *unlink*).
The forced namespace disambiguates them at the point of use.

### Config (`config` namespace — unchanged shape)

```
config show | edit | init | identity | refresh | migrate | backup | restore
```

The shape is already correct. No changes beyond what falls out of
the migration (help-text consistency).

### Wiring (`environment` + `sources` — the sensor patch panel)

The pair of namespaces that bridge installed software to the sensor
feeds it consumes. `environment` enumerates the peers (declared in
`environment.toml`, reconciled against live discovery); `sources`
selects, per client, which of those peers it consumes from.

```
environment list | probe | describe         # what peers exist (declared vs observed)
sources     list | add  | remove | apply    # per-client selection from that inventory
```

The verb shape under `sources` is unchanged from today. Operator
cadence: touched any time a sensor moves on the LAN, a new RX-888 or
magnetometer is added, or a recorder needs repointing. *Not* an
installer-only verb — that misreading is what the v1 inventory
implied with its "apt + pip source management" row, which was wrong
(a "source" here is a sensor feed, not an apt mirror).

#### Source-kind taxonomy

A source-key is `<kind>:<id>`. Today's kinds are HF-RF only
(`radiod:<id>` for ka9q-radio control planes, `kiwi:<id>` for
KiwiSDRs). The taxonomy is not RF-bounded — it's any sensor feed a
recorder consumes — and is expected to grow:

| Kind | Consumed by | Status |
|---|---|---|
| `radiod` | wspr-recorder, psk-recorder, hfdl-recorder, codar-sounder | present |
| `kiwi` | wspr-recorder, psk-recorder (KiwiSDR-fed paths) | present |
| `mag` | mag-recorder | planned — landing as mag-recorder matures |
| `vlf` | future VLF recorder client | future — placeholder for an unwritten client |

GPSDO is already a peer kind in `environment` (`--kind gpsdo`); it
becomes a `sources` kind only if/when a recorder client consumes
GPSDO PPS / NMEA directly (e.g. a `gpsdo-monitor` recorder).
Until that exists, GPSDO stays peer-only.

#### Environment `--kind` must grow with the taxonomy

`environment list --kind` and `environment probe --kind` accept a
closed `choices=[...]` list today (`radiod / kiwisdr / gpsdo /
time_source`). The migration must keep this in step with the
source-kind table above as new kinds land — adding `mag` when
mag-recorder is wired into `sources`, `vlf` when a VLF recorder
exists, and so on. A divergence (kind valid for `sources` but
unknown to `environment --kind`) would break the discoverability
contract the Wiring cluster relies on.

### Diagnostics (`diag` namespace)

```
diag cpu-affinity [--apply]
diag cpu-freq    [--apply]
diag net         [--listen]
```

`cpu` (the bare shortcut to `diag cpu-freq`) is **removed**. No
alias. Symmetric with `affinity` and `net`, which never had bare
shortcuts.

### Installation-time tuning (mixed namespaces)

```
wisdom plan | status
storage migrate-to-sqlite | trim | tune-timestd
```

`timestd-tune-storage` (today's top-level three-token bare verb) is
**renamed** to `storage tune-timestd`. The work it does is storage
sizing; it belongs under `storage`.

The `storage` namespace has mixed cadence — only `migrate-to-sqlite`
and `tune-timestd` are installation-time. Namespace cohesion (one
parser group) wins over splitting across clusters:

| Sub-verb | Cadence | Primary invocation |
|---|---|---|
| `storage migrate-to-sqlite` | One-shot per host (ClickHouse → SQLite migration) | Manual, during bring-up |
| `storage tune-timestd` | One-shot sizing helper | Manual, during bring-up |
| `storage trim` | **Scheduled (systemd timer)** — TTL janitor for `pending_uploads` (`cmd_storage_trim`, smd:6461) | systemd timer; the CLI is the ad-hoc / dry-run path |

`storage trim` is in this cluster only because the namespace stays
intact — its actual cadence is daily maintenance, not bring-up.

The `wisdom` namespace has the same pattern: `wisdom plan` is the
installer-time one-shot (hours of FFTW3 planning on first run);
`wisdom status` is an operator-visible read query ("did the planner
finish yet"). `status` is kept in this cluster for namespace cohesion
rather than split into Observation.

`sources` is **not** in this cluster despite first appearances —
see §3 Wiring.

### Data quality (`verifier` namespace — unchanged shape)

```
verifier report [--target wspr|psk]
verifier rehabilitate
```

### Meta

```
tui                              # launch interactive UI (universal entry: config edit + lifecycle + monitoring)
completion bash
```

`tui` is in Meta — not Observation — because the TUI is the
universal entry point: it surfaces config edit, lifecycle, *and*
monitoring. Bucketing it as "read-only observation" understates what
it is.

---

## 4. Aliases preserved

The full keep-list. Anything not on this list is either canonical or
removed.

| Alias | Canonical | Reason for keeping |
|---|---|---|
| `list` | `component list` | Highest-frequency operator browse verb; the `--help` cost of forcing `component list` outweighs the namespace-clarity win |
| `install` | `component install` | Documented in install-quickstart.md as the bring-up verb; breaking it churns external docs |
| `enable` | `component enable` | Bare verb shape matches systemctl muscle memory even though the underlying mutation is to `topology.toml` (catalog), not systemd units |
| `disable` | `component disable` | Same as `enable`; `cmd_disable` additionally issues `systemctl stop` as a side-effect of the catalog toggle |

Aliases are real subparsers that dispatch to the same handlers as the
canonical form. Help text on the alias says `(alias for <canonical>)`
so the namespace is discoverable from `smd list --help`.

---

## 5. Removed verbs

Verbs deleted outright in v2. Migration commit emits a deprecation
warning for one release before deletion.

| Verb | Replacement | Removal rationale |
|---|---|---|
| `psk-watch` | `watch psk` | Legacy single-target; duplicates `watch <target>` |
| `wspr-watch` | `watch wspr` | same |
| `hfdl-watch` | `watch hfdl` | same |
| `codar-watch` | `watch codar` | same |
| `ka9q-watch` | `watch ka9q` | same |
| `software install` | `install` (= `component install`) | Rewrites to top-level today; pure indirection |
| `software apply` | `apply` | same |
| `software` (group) | — | No real members once `install`/`apply` go |
| `cpu` | `diag cpu-freq` | Asymmetric shortcut for one of three diag entries |
| `add` (bare) | `component add` | Confusable with package-add; rare installer verb |
| `remove` (bare) | `component remove` | same |
| `timestd-tune-storage` | `storage tune-timestd` | Three-token bare-verb outlier; belongs in `storage` |
| `list --update` | `component update` | The `list` verb shouldn't carry a mutating flag |
| `list --apply` | `component update --apply` | same |
| `log --level <LEVEL>` | `log set-level <client> <level>` | Mutating flag on a read verb — same anti-pattern as the old `list --update`, replaced with `component update` |
| `'update'` in `_MUTATING` | — | Already dead (no subparser of that name); pure cleanup |

Net change: **33 → 24 top-level verbs**, with two of the surviving
24 (`list`, `install`) being canonical-namespaced-form aliases.

---

## 6. Overloads tolerated (no rename)

These are *not* changed in v2 even though they could be argued about.
Recording the decision so it isn't re-litigated during migration.

| Surface | Why it stays |
|---|---|
| `apply` as top-level vs. `sources apply` vs. `diag * --apply` vs. `config migrate` (which applies) | Each `apply` is unambiguous in context. Renaming `sources apply` → `sources sync` would help newcomers but breaks installer docs; not worth it now. |
| Top-level `apply` verb name (vs. `reload` / `reconcile` / `sync`) | `reload` is taken (per-service SIGHUP, matches `systemctl reload`); `daemon-reload` is awkward and semantically wrong (systemctl `daemon-reload` is informational re-read, `cmd_apply` actively reconciles and enacts). `reconcile` is too long for daily ops; `sync` is ambiguous in direction. `apply` matches kubectl / terraform / helm muscle memory and the actual reconcile-and-enact semantic. |
| `wisdom` (domain-opaque verb name) | Load-bearing for the HamSCI operator community; FFTW wisdom is the term of art. |
| `environment list` / `component list` / `sources list` | Three `list`s, all read-only, all scoped by namespace. Standard pattern. |
| `config init` (per-client wizard) vs. `install` (catalog walk) | Different objects (config file vs. software). Both names are correct in their domain. |

---

## 7. Open questions deferred to migration

Decisions the migration commit can make without re-opening §3:

1. **Help-text taxonomy.** `--help` currently lists verbs
   alphabetically. The migration should group them by cluster
   (argparse `add_argument_group`-style or a custom formatter) so
   the persona model is visible at the first `smd --help`.
2. **Bash completion regeneration.** `completion bash` output must
   reflect the new shape (canonical + alias) and emit nothing for
   removed verbs.
3. **Deprecation warning channel.** Stderr line on invocation of a
   removed/deprecated verb, naming the replacement. One release of
   warnings, then deletion.
4. **`component update` semantics.** Today's `list --update` updates
   *all* installed components per their version policy. The
   namespaced form should accept an optional `<name>` to update one,
   matching `install [name]`.
5. **Whether `component update` deserves a bare alias.** Operator
   cadence will tell. Defer; leave it namespace-only at first.
6. **`log set-level` parser shape.** The new sub-verb lives as a
   subparser under `log`, not as a top-level verb. The bare `log
   [<client>]` form (today's default) stays as the read entry;
   `log set-level <client> <level>` is added as a sibling
   subparser. The `--level` flag on `log` is removed (after the
   one-release deprecation window from §5).

---

## 8. Out of scope

- Behaviour changes to any handler. v2 is a verb-surface rework
  only; `cmd_*` bodies are not touched except where a rename
  forces it (e.g., `cmd_timestd_tune_storage` lives in the same
  function, just reached via `storage tune-timestd`).
- The remaining capability gap from §3 of the inventory
  (identity/refresh in TUI). Four of the original five gaps closed
  in follow-up commits: the `sources` screen (list + apply;
  add/remove still CLI), the `activity` screen (live tail of `smd
  watch <target>` for all seven targets via a single screen with a
  target selector), the `verifier` screen (report + rehabilitate
  combined because they share an operator workflow), and Apply
  buttons on the `cpu_affinity` and `cpu_freq` screens (each runs
  the matching `smd admin diag cpu-* --apply` via confirm modal,
  auto-refreshes on success). v2 cleans up the CLI; the remaining
  TUI gap-fill is a separate track.
- A `pkg` synonym for `component`. Considered, rejected — `component`
  is the term used in catalog.toml, topology.toml, and the existing
  TUI screen labels; introducing a synonym would fragment the
  vocabulary.
- Any change to client repos' own CLIs (psk-recorder,
  wspr-recorder, hfdl-recorder, etc.). This spec covers `bin/smd`
  only.

---

## 9. Amendment — `admin` umbrella (supersedes the top-level placement in §3)

The v2 surface above kept diagnostic / maintenance / occasional verbs
at the top level (grouped by persona but flat). Operator feedback was
that the top level was still too crowded: the daily surface is a
handful of verbs, and everything else is reached rarely. This
amendment introduces a single **`smd admin`** umbrella and relocates
the occasional verbs underneath it, shrinking the top level to the
small daily set. It **supersedes** the top-level placement of the
moved verbs in §3 (Diagnostics, Wiring, Installation-time tuning,
Data quality, and the `log`/`public-ip` Observation members); their
*shapes* are unchanged — only the `admin ` prefix is added.

### Core (stays top-level)

```
status  watch  tui
start  stop  restart  reload   apply   enable  disable
install   list   component   config   bringup
```

### Moved under `admin` (so `smd diag` → `smd admin diag`, etc.)

```
diag  validate  verifier  wisdom  storage  environment  sources
public-ip  log  rac  timing  radiod  instance  uninstall  completion
```

Each keeps its own sub-subparsers (`admin diag cpu-affinity`,
`admin storage trim`, `admin log set-level`, `admin completion bash`,
…). Bare `smd admin` prints the umbrella's help.

### Removed (hard, no shim) — the §5 removals, now executed

```
cpu  add  remove  software  timestd-tune-storage
```

### Migration stance — hard remove, no deprecation shims

Unlike §5's one-release-warning plan, the moved verbs were removed
from the top level **outright** (no deprecation alias). Consequences,
all handled in the migration commit:

- **Internal callers updated.** Every `[_smd_binary(), '<verb>', …]`
  call in the TUI screens now passes `'admin'` first; `tui_walk.py`
  assertions updated to match.
- **Full reference sweep.** ~210 textual references to the old
  top-level forms — handler help strings, runtime error/remediation
  messages, generated-config guidance comments
  (`instance.py`/`sources.py`/`storage_migrate.py`), docstrings, this
  `docs/` set, `CLAUDE.md`, and tests — were rewritten to the
  `smd admin …` path.
- **Completion + auto-load.** `completion` itself moved, so the bash
  completion script and the operator alias auto-load
  (`etc/aliases.sh`) now use `smd admin completion bash`; the shipped
  `smdrefresh` helper too.

The trade the operator accepted: any *external* script or muscle
memory using a bare moved verb breaks with an argparse "invalid
choice" error rather than a one-release warning.
