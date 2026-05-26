# radiod identification — canonical multicast naming

Locks the radiod-naming model that all sigmond-suite clients (psk-recorder,
wspr-recorder, hfdl-recorder, codar-sounder, hf-timestd) and the sigmond
substrate (coordination.toml, validate rules, configurators) must converge
on. Companion to [CLIENT-CONTRACT.md](CLIENT-CONTRACT.md) and
[MULTI-INSTANCE-ARCHITECTURE.md](MULTI-INSTANCE-ARCHITECTURE.md).

The mechanical migration (per-client schema changes, configurator rewrites,
host data backfill) is staged in §6.

Source state: this repo at commit `67b0c6a` (2026-05-26) — diagnostic
skip-message commit for `rule_frequency_coverage`, which surfaced the
naming-divergence problem on bee1.

---

## 1. Why this matters

bee1's `smd validate` after the multi-instance migration showed:

```
frequency_coverage: 1 frequency claim(s) within samprate
```

Only **1 frequency claim** — out of 42 (19 psk + 16 wspr + 7 hfdl) that the
recorders publish via inventory. The other three recorders' frequencies
weren't reaching the validator.

Root cause: same physical radiod, three different labels in client configs:

| Surface | Label |
|---|---|
| `/etc/radio/radiod@ac0g-bee1-rx888.conf` (file name) | `ac0g-bee1-rx888` |
| `/etc/psk-recorder/AC0G-B1.toml` `[[radiod]].id` | `my-rx888` |
| `/etc/hfdl-recorder/AC0G-B1.toml` `[[radiod]].id` | `my-rx888` |
| `/etc/codar-sounder/AC0G-B1.toml` `[[radiod]].id` | `ac0g-bee1-rx888` |
| `/etc/wspr-recorder/AC0G-B1.toml` `[radiod].status_address` | `bee1-status.local` |
| coordination.toml `[radiod."..."]` block key | `ac0g-bee1-rx888` |
| **All four clients' actual mDNS target** | `bee1-status.local` ✓ |

Each client picked a different LOCAL label for the same physical radiod.
The labels are operator-chosen, opaque, and have no functional meaning to
the daemons — at runtime every client connects to the same mDNS-broadcast
control/status stream `bee1-status.local`. The labels exist only because
the schema requires `id`.

`rule_frequency_coverage` iterates `coord.radiods.items()` and for each
radiod `rid`, picks consumers whose `iv.radiod_id == rid`. Three different
labels means three of the four consumers don't match any coordination block,
and their `frequencies_hz` lists never get checked.

---

## 2. The model — multicast control/status name is the only functional ID

Per the operator design intent (Rob's note 2026-05-26):

> The radiod source should always be one of the mDNS-broadcasted and
> discovered radiod instances on the local network. This is only
> predetermined in the sense that a user has to know the name of the
> status/control stream of the radiod she wants to connect to.

Three categories of name, exactly one of which is functional:

| Name | Owner | Functional? | Used at runtime? |
|---|---|---|---|
| radiod's config-file basename (e.g. `ac0g-bee1-rx888`) | operator (bookkeeping) | no | no — radiod's filesystem-side metadata only |
| client's `[[radiod]] id` local label (legacy) | operator (per-client) | no | no — opaque to ka9q-python |
| **mDNS control/status multicast name (e.g. `bee1-status.local`)** | radiod (broadcast) | **yes** | **yes** — ka9q-python resolves this to connect |

ka9q-python can enumerate all radiod instances broadcasting on the LAN.
Client configuration is reduced to selecting one multicast name from the
discovered set; everything else (multicast group address, sample rate,
channel count) flows from there.

The radiod config-file basename remains an operator convenience for naming
the .conf file under `/etc/radio/`. It does not appear in any client
config, inventory output, or coordination.toml.

---

## 3. Schema changes — locked

### 3.1 Client config (psk-recorder, wspr-recorder, hfdl-recorder, codar-sounder)

Replace `[[radiod]] id` with `[[radiod]] status`. The field's value is the
multicast control/status name (e.g. `bee1-status.local`).

```toml
# Old (legacy — deprecated)
[[radiod]]
id            = "my-rx888"             # arbitrary local label
radiod_status = "bee1-status.local"    # the actual mDNS target

# New
[[radiod]]
status = "bee1-status.local"           # mDNS control/status, single source of truth
```

For clients with a singleton `[radiod]` block (e.g. wspr-recorder):

```toml
# Old
[radiod]
status_address = "bee1-status.local"

# New
[radiod]
status = "bee1-status.local"
```

Rationale for `status` (not `multicast`, not `id`): matches the field name
in radiod's own `.conf` file (`[control] status = ...`); already familiar
to operators reading the radiod side; short and unambiguous.

The legacy fields (`id`, `radiod_status`, `status_address`, `status_dns`)
become deprecated. Daemons accept either form for one CONTRACT release;
emit `DeprecationWarning` on load when only the legacy form is present.
Drop legacy acceptance one release after.

### 3.2 Inventory output (CLIENT-CONTRACT.md §20 — bump to v0.9)

Per-instance inventory entries publish `radiod_id` = the multicast status
name they're configured to consume from:

```json
{
  "instances": [{
    "instance": "AC0G-B1",
    "radiod_id": "bee1-status.local",
    "frequencies_hz": [1840000, 3573000, ...]
  }]
}
```

The field name remains `radiod_id` for backward compatibility with the
sigmond.clients.InstanceView dataclass and the existing validator rules
that read it. Only the VALUE changes: from local label to multicast name.

### 3.3 coordination.toml

Block keyed by multicast name:

```toml
# Old
[radiod."ac0g-bee1-rx888"]
status_dns  = "bee1-status.local"
samprate_hz = 129600000

# New
[radiod."bee1-status.local"]
samprate_hz = 129600000
cores       = "0-7"
# `status_dns` field removed; block key IS the status name
```

The block key being the multicast name means `coord.radiods.keys()` is the
list of declared status names, and `_consumers_of(view, "bee1-status.local")`
finds every client whose inventory reports that name as `radiod_id`.

Sigmond's `Radiod` dataclass keeps its `id` field (= the dict key); the
field is documented as "the mDNS control/status multicast name" rather
than "an arbitrary identifier."

### 3.4 Pre-configuration templates

Each client's config template (e.g.
`<repo>/config/<client>-config.toml.template`) uses an explicit
placeholder for the status field:

```toml
[[radiod]]
# REQUIRED: the mDNS control/status name of the radiod to consume.
# Discover available radiods with: ka9q-python list  (or the configurator
# wizard, which fills this in automatically).
status = "<configure-via-config-init>"
```

Daemons refuse to start when they see the literal `<configure-...>` token;
operators are pointed at `<client> config init`.

---

## 4. The configurator wizard

Each client's `<client> config init` (and `config edit`) becomes
discovery-driven. The flow:

1. **Discover**: call ka9q-python's enumeration (whatever the canonical API
   is — TBD during implementation) to list radiods broadcasting on the LAN.
2. **Zero-radiod case**: print
   ```
   No radiod instances are broadcasting on the local network.
   Install and start radiod first:  smd install ka9q-radio
   ```
   and exit nonzero. No config is written. The pre-config template still
   contains the placeholder.
3. **Single-radiod case**: prompt `Use <name>? [Y/n]`. Write that into
   `[[radiod]] status`.
4. **Multi-radiod case**: present a menu listing each discovered radiod
   with its multicast name, broadcasting host, samprate (if available),
   and channel capacity. Operator picks one. Write it.
5. **Migration case** (legacy config detected): if `[[radiod]] id` is
   present without `status`, the wizard reads the legacy `radiod_status` /
   `status_address` / `status_dns` field, confirms via ka9q-python that
   the named multicast is currently discoverable, and rewrites the block
   to use `status` only. Removes legacy fields. Logs a one-line summary
   of what changed.

This is the single source of truth for radiod selection. Operators don't
edit the field by hand; the wizard owns it.

---

## 5. Validate rule updates

Existing rule behavior, with the new schema:

- `rule_radiod_resolution`: client's inventory `radiod_id` (= multicast
  name) must match a coordination.toml `[radiod.<name>]` block.
  Implementation unchanged; semantics now meaningful because both sides
  use the multicast name.
- `rule_frequency_coverage`: as today, but now actually fires because
  consumer-to-radiod matching works.
- `rule_cpu_isolation`: unchanged.
- `rule_timing_chain`: unchanged.

Optional new rule (deferred to a follow-up):
- `rule_radiod_discoverable`: each `[radiod.<name>]` block declared in
  coordination.toml should resolve via ka9q-python at validate time.
  Warns if declared-but-not-discoverable.

---

## 6. Implementation phases — locked

**Phase 1 — Sigmond foundation.**
- `sigmond.coordination.Radiod`: documentation update; the `id` field
  is the mDNS multicast name.
- `coordination.toml` schema: block keyed by multicast name; `status_dns`
  field becomes optional alias (deprecated; reads as `id` if present and
  block key differs from it).
- Validator rules: no logic change required; semantics improve once
  client inventories align (Phase 2).
- Tests + a `smd validate` golden output check.

**Phase 2 — Client inventory output (per repo).**
- Each of psk/wspr/hfdl/codar/hf-timestd emits `radiod_id` = the
  multicast name they consume from, read from the existing
  `radiod_status` / `status_address` / `status_dns` field.
- CONTRACT v0.8 → v0.9 — new §20 documents the rule.
- Tests updated.
- No daemon-behavior change; no config-schema change.

After Phase 2, bee1's `smd validate` would show 42 frequency claims
checked instead of 1, with no operator action required beyond rerunning
each client to refresh its inventory output.

**Phase 3 — Client config schema (per repo).**
- Rename `[[radiod]] id` → `[[radiod]] status` (new field).
- Daemons accept either form during one CONTRACT release.
  - When both present: `status` wins, `id` is ignored with warning.
  - When only `id` present: read it; emit DeprecationWarning.
  - When only `status` present: clean new path.
- Other legacy fields (`radiod_status`, `status_address`, `status_dns`)
  also accepted during deprecation; merged into `status` on load.
- Config templates use the placeholder token (§3.4).

**Phase 4 — Configurator wizards (per repo).**
- Each `<client> config init` / `config edit` uses ka9q-python discovery.
- Single-discovery zero/one/multi-radiod flow (§4).
- Migration path for legacy configs (§4 step 5).

**Phase 5 — Per-host migration.**
- `smd radiod migrate` walks each enabled client's config on the host
  and rewrites legacy `id` blocks to the new `status` form.
- Equivalent to running `<client> config edit` per client but
  scriptable + idempotent.
- Also rewrites coordination.toml's `[radiod."<old>"]` block key to
  the multicast name.
- Dry-run by default; `--yes` to apply.

**Phase 6 — Cutover.**
- One release after Phase 4 ships, remove legacy field acceptance from
  client daemons. `[[radiod]] id` becomes an error.

---

## 7. Out of scope for this spec

- Changes to radiod's own configuration. The radiod side is upstream
  (ka9q-radio); sigmond consumes whatever multicast names it broadcasts.
- The radiod config-file basename remains an operator convenience under
  `/etc/radio/radiod@<name>.conf`. The choice of basename has no
  client-visible effect.
- ka9q-python's discovery API itself. This spec assumes the library
  provides enumeration; if it doesn't, that's a prerequisite that lives
  in ka9q-python.
- mag-recorder. Uses a USB magnetometer, not a radiod source. No
  changes.
- gpsdo-monitor / hf-timestd's per-radiod governor binding. Already
  uses the multicast name semantics by virtue of mDNS-based discovery;
  no schema rename needed (verify in Phase 2).

---

## 8. Open questions deferred to implementation

1. **ka9q-python discovery API shape.** Does the library already expose
   a single function call that returns `[{"multicast": ..., "host": ...,
   "samprate": ...}]`? If not, Phase 4 has a prerequisite.
2. **Configurator multi-radiod menu UX.** Whiptail menu, plain-text
   prompt, or a Textual chooser inside `smd tui`? Defer to per-client.
3. **`smd radiod migrate` scope.** Does it also stop and restart the
   affected daemons after rewriting their configs? Lean: yes, with a
   `--no-restart` flag for operators who want to batch.
4. **Multi-radiod-per-client.** psk-recorder's `[[radiod]]` is an array
   (per-instance can consume from multiple radiods). The Phase 3 schema
   keeps the array form; each entry just has `status` instead of `id`.
   wspr-recorder's singleton stays singleton.
