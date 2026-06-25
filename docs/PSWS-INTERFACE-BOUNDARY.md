# The Sigmond ⇄ PSWS Interface Boundary

**Purpose.** This document defines the seam between **sigmond** (the
station-edge implementation) and the **PSWS network** (the
HamSCI/UA-led database, API, and visualization layer described on
[board #6](https://github.com/orgs/HamSCI/projects/6)). It is the
companion to [PSWS-MAPPING.md](PSWS-MAPPING.md): the mapping says *which*
board items sigmond touches; this doc says *exactly where the line is*
for the **shared** ones, because nearly every scope dispute reduces to
"which side of the upload does this live on?"

If you only read one section, read **§2 (the boundary principle)** and
**§3 (the interface surface as it exists today)** — together they decide
most questions mechanically.

---

## 1. Why an explicit boundary

Sigmond and the PSWS server are built by different people on different
schedules. Where they meet is a small, concrete set of upload paths.
Left implicit, that seam becomes the place where features fall through
("I assumed the station did the QA" / "I assumed the server deduped").
Written down, the seam becomes a **contract**: each side knows what it
emits, what it consumes, and what it may assume about the other. The
contract also gives the Monday-meeting negotiation a fixed object to
point at instead of re-litigating scope per feature.

## 2. The boundary principle

> **Sigmond owns everything that must happen *on the station, before or
> at upload*.** PSWS owns everything that happens to *aggregated data
> after ingest*. The two touch only at the **upload interface**:
> `hs-uploader` transports + the records they carry + the
> registration/identity handshake that authorizes them.

Corollaries:

- **Local-truth, then ship.** The shared SQLite sink
  (`/var/lib/sigmond/sink.db`) is the station's source of truth.
  Uploads are *derived* from it; the station never depends on the server
  to know its own state. (This mirrors the same "durable shared state is
  truth; notifications are hints" principle the sink uses internally —
  see `CLAUDE.md` → "Cross-process upload wake.")
- **The station can run headless and disconnected.** Anything that
  *requires* the server to be reachable in order for acquisition to
  proceed is on the wrong side of the line.
- **Presentation, cross-station joins, and long-term storage are never
  sigmond's.** Imaging across stations, Madrigal/HAPI APIs, websites,
  and plotting live upstream by definition.

## 3. The interface surface as it exists today

This is the *actual* wire surface, from the code — not aspirational.

### 3.1 Producer side — the sink queue

`sigmond.hamsci_sink.Writer` (in `sigmond/lib/sigmond/hamsci_sink/`)
stages every uploadable record into one queue table in
`/var/lib/sigmond/sink.db`:

```
pending_uploads(
    id              INTEGER PRIMARY KEY,
    target_db       TEXT,     -- logical stream, e.g. "psk", "wspr", "timestd"
    target_table    TEXT,     -- e.g. "spots", "noise", "events"
    schema_version  INTEGER,  -- producer's schema version, per row
    payload_json    TEXT,     -- the record
    queued_at        ...
)
```

`(target_db, target_table)` names the stream; `schema_version` travels
**with every row** so a transport can refuse data it would misread (see
§3.4). This queue is the **only** thing a client must produce to
participate in upload — it does not talk to the network itself.

### 3.2 Consumer side — `hs-uploader`

`hs-uploader` is the read-side counterpart. Three orthogonal
abstractions (see `hs-uploader/README.md`):

- **Source** — yields `Record`s from an opaque cursor. `SqliteSource`
  reads the `pending_uploads` queue (preferred); `WsprCycleSource`
  bundles one WSPR cycle's `spots`+`noise` per tar; `FileTreeSource` is
  the file-spool fallback.
- **Transport** — one per upstream destination; accepts a batch, returns
  an `Outcome` (acked / partial-ack / retry-later / dead).
- **WatermarkStore** — SQLite-backed per-`(source, destination, table)`
  cursor + retry state. Restart re-derives the batch from the cursor, so
  there is no in-flight state to lose — uploads are **idempotent**.

The library is synchronous and idempotent by design; this is what makes
the boundary safe across restarts on either side.

### 3.3 The PSWS-specific transports

Two destinations on the interface are *the PSWS server itself* (the rest
— wsprnet, wsprdaemon, pskreporter — are other communities and out of
scope for this boundary):

- **`PswsMagnetometerSftp`** (`hs-uploader/.../transports/psws_magnetometer.py`)
  — SFTPs a Grape-style zip to `pswsnetwork.eng.ua.edu` and `mkdir`s a
  Grape-style trigger directory. Authorizes as:
  - `sftp_user` = **PSWS station id** (e.g. `S000082`), defaulting to
    `identity.station_id`.
  - `ssh_key_file` = the private key **registered on the PSWS portal**,
    defaulting to `identity.ssh_key_file` (shared with the Grape upload
    path).
  - `remote_path` / instrument id locate the product on the server.
  This is the concrete shape of board item **#25 (registration)** and
  **#5 (WW0WWV → PSWS migration)**.
- **Phase-D PSK merge endpoint** (`docs/PHASE-D-SERVER-MERGE-ENDPOINT.md`,
  *plan, server side not yet implemented*) — defines the cross-receiver
  JSONL contract (`ft8/<site>/<rx>/<band>/<cycle>_ft8.jsonl`, per-row
  fields incl. `rx_source`, `host_call`, `radiod_id`) for when a station
  ships every receiver's row tagged for the server to dedup. Documented
  here because it is a *future* widening of the interface.

### 3.4 The schema-version contract

Every queue row carries the producer's `schema_version`. `hs-uploader`'s
pipeline filters rows outside its accepted set and flips source health to
`stale-schema` — **a clean halt, not a silent misread.** This is the
interface's versioning discipline: producers bump `schema_version` on any
payload change; transports/servers declare the versions they accept;
nothing ships across a mismatch. **Preserve this** — it is what lets the
two sides evolve independently without corrupting the database.

## 4. Applying the boundary to the shared items

For each **shared** board item (from PSWS-MAPPING.md), here is where the
line falls:

| Board item | Station side (sigmond) | Server side (PSWS) | Line falls at |
|-----------|------------------------|--------------------|---------------|
| #25 Registration | Hold `station_id` + portal-registered SSH key in `identity`; present them on SFTP. | Issue station id; accept/registry the public key; bind to metadata. | The portal handshake that mints the id + trusts the key. |
| #5 WW0WWV migration | Ship via `PswsMagnetometerSftp` to `pswsnetwork.*`, not WW0WWV FTP. | Accept the Grape-style upload + trigger. | The SFTP destination + trigger convention. |
| #8/#21/#22 QA & feedback | Run within-band / harmonization checks pre-upload; emit QA metadata + `validate` flags. | Aggregate QA, raise operator-facing flags, present feedback. | QA *metadata fields* carried alongside data rows. |
| #19 Level-0 from station cache | Hold full-res data in the sink; serve a Level-0 batch on request. | Place the request; receive on next heartbeat. | A **request/response** extension to the heartbeat (not yet built). |
| #20/#39 Network health & availability | Emit `inventory --json` / upload heartbeat. | Aggregate into the health query + availability (Gantt) view. | The **heartbeat payload** (see §5). |
| #36/#15 Calibration | Measure signal+noise at SMA (`wspr-recorder`). | Define + apply the calibration standard. | The calibrated-level *fields* + their units. |

## 5. Parts of the contract that are not yet real interfaces

Be honest about what is *named* but not yet *specified*. These are the
negotiation targets, not finished surfaces:

- **The heartbeat.** The board's network-health query, station-availability
  view, down-alerts, and "request Level-0 on next heartbeat" items all
  assume a station→server heartbeat carrying health/availability and
  accepting requests. Today's interface is *upload-only* (push). **This
  is now specified** as a concrete proposal in
  [PSWS-HEARTBEAT-SPEC.md](PSWS-HEARTBEAT-SPEC.md) — a station-level
  roll-up of the existing `inventory --json` contract, reusing the PSWS
  identity, with an optional response channel for `level0_pull`. It is
  the first request/response widening of this boundary; the server side
  is UA's to build.
- **Registration metadata.** #25 covers keys today; the *metadata*
  half (station capabilities, instrument list, PII-compliant location
  per #10) is not yet a defined payload.
- **QA metadata fields.** #21/#22 need an agreed *schema* for
  per-product QA flags before the station can emit them and the server
  can store them. Until then it's two halves with no shared field set.

When any of these is specified, it becomes a new `target_db`/payload (for
push data) or a new transport/endpoint (for request/response), and gets a
`schema_version` from day one.

## 6. Change discipline

- **Adding a stream:** new `(target_db, target_table)` + a starting
  `schema_version`. No server coordination needed for destinations that
  ignore it; coordinate with PSWS for PSWS-bound streams.
- **Changing a payload:** bump `schema_version`; never reuse a version
  for a changed shape. The `stale-schema` halt will protect the server
  until it declares acceptance.
- **Adding a PSWS destination:** new `Transport` in `hs-uploader`,
  authorized through `identity` (station id + key). Document it in §3.3.
- **Widening to request/response (heartbeat, Level-0 pull):** specify it
  here *before* implementing — it is the one place the push-only
  assumption breaks, and both sides must agree on the channel.

> Rule of thumb: if a proposed feature does not change the sink queue,
> a transport, the identity handshake, or a (future) heartbeat, it is
> **not** on this boundary — it lives wholly on one side. Use that to
> short-circuit scope debates: locate the feature's interface change
> first; if there is none, the owner is already decided.
