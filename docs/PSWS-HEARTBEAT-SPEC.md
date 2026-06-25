# Station→PSWS Heartbeat — Interface Spec (proposal)

**Status: plan, not implemented.** This specifies the one interface the
[boundary doc](PSWS-INTERFACE-BOUNDARY.md) §5 flags as *named but not yet
real*: a station→PSWS **heartbeat** carrying health / availability /
provenance, and (optionally) accepting requests back. It is the first
**request/response** widening of an interface that is **push-only**
today. The server side is UA-owned, so this doc is a concrete proposal to
bring to the Monday PSWS meeting, not a unilateral design.

It exists to unblock several board items that all silently assume this
channel: **Database query / network health**, **station-availability /
Gantt view**, **Request Level-0 from station cache**, and the new
**per-station timing-provenance (tier)** issue
([#50](https://github.com/HamSCI/psws-charette/issues/50)).

---

## 1. Design principles

1. **Reuse the contract, don't invent a payload.** Every sigmond client
   already emits `inventory --json` and `validate --json` (CLIENT-CONTRACT.md
   §3), and `smd status` already aggregates them across the station. The
   heartbeat is that aggregation, shipped on a cadence. No new producer
   work in the clients.
2. **The heartbeat is the one *station-level* product.** Data uploads are
   per-instrument / per-stream (`pending_uploads.target_db`). The
   heartbeat is one envelope **per station**, rolling up all clients —
   a genuinely new record shape, not another `target_db`.
3. **Push-derived health; server derives availability.** The station
   reports *what it sees about itself*. **Liveness/availability is
   derived server-side from heartbeat arrival times** — a missing
   heartbeat is the down signal. The station never depends on the server
   to know its own state (mirrors the sink's "durable state is truth"
   rule, CLAUDE.md → Cross-process upload wake).
4. **Reuse the existing PSWS identity.** Authenticate with the same
   `station_id` + portal-registered SSH key that `PswsMagnetometerSftp`
   already uses (board #25). **No new credential story.**
5. **PII-respecting (board #10).** Location is reported at reduced
   precision only — 6-char Maidenhead grid or lat/lon to 2 decimals —
   never finer, regardless of what the station knows.
6. **Versioned and idempotent.** The envelope carries `schema_version`
   from day one (same discipline as the sink queue). Heartbeats are
   stateless snapshots; a lost or duplicated one self-heals on the next
   tick.
7. **Degrades offline.** If the channel is unreachable the station keeps
   acquiring and uploading; heartbeats are best-effort and the server's
   availability view simply shows the gap.

## 2. The payload (station→server)

One JSON envelope per station per tick. Fields are drawn directly from
the existing inventory contract; nothing here is new producer-side.

```json
{
  "schema_version": 1,
  "kind": "station_heartbeat",
  "station_id": "S000082",
  "callsign": "AC0G",
  "grid": "EM38",
  "emitted_at": "2026-06-24T19:42:05Z",
  "sigmond_version": "…",
  "uptime_s": 864000,

  "timing": {
    "authority": "hf-timestd@bee3",
    "tier": "T5",
    "sigma_ns": 1200,
    "snapshot_age_s": 4.2,
    "gpsdo": {"locked": true, "source": "lb-gpsdo"}
  },

  "instruments": [
    {
      "client": "hf-timestd",
      "version": "7.0.0",
      "contract_version": "0.7",
      "git": {"short": "96beda9", "ref": "main", "dirty": false},
      "state": "active",
      "instances": [
        {
          "instance": "default",
          "radiod_id": "bee3-rx888",
          "frequencies_hz": [2500000, 3330000, 5000000],
          "ka9q_channels": 9,
          "modes": ["wwv", "wwvh", "chu", "bpm"],
          "timing_authority_applied": {
            "source": "hf-timestd@bee3", "tier": "T5",
            "sigma_ns": 1200, "snapshot_age_s": 4.2
          }
        }
      ],
      "issues": []
    }
  ],

  "health": {
    "ok": true,
    "issues": [
      {"severity": "warn", "client": "psk-recorder",
       "instance": "default", "message": "storage_quota above 90%"}
    ]
  },

  "uploads": {
    "pending_count": 1843,
    "last_acked": {
      "wspr": "2026-06-24T19:40:00Z",
      "psk":  "2026-06-24T19:41:53Z"
    }
  },

  "cache": {
    "available_targets": ["wspr", "psk", "timestd", "mag"],
    "level0_window_s": 604800
  }
}
```

Field provenance — every field maps to something sigmond already has:

| Envelope field | Source in sigmond |
|----------------|-------------------|
| `station_id`, `callsign`, `grid` | station `identity` (the PSWS SFTP user + PII-reduced location) |
| `timing.*` | `hf-timestd` authority + `gpsdo-monitor` (the same data behind issue #50) |
| `instruments[]` | each client's `inventory --json`, summarized (this is what `smd status` already collects) |
| `instruments[].timing_authority_applied` | verbatim from inventory v0.7 (CLIENT-CONTRACT §3) |
| `health.issues[]` | union of each client's `validate --json` issues |
| `uploads.*` | `hs-uploader` watermark store + `pending_uploads` depth |
| `cache.*` | sink `target_db`s present + retention window (storage-trim policy) |

## 3. The channel

Two viable transports; **recommend (A), keep (B) as the zero-new-auth
fallback.** Final call is UA's since they build the server endpoint.

**(A) HTTPS POST — recommended.** Station POSTs the envelope to a PSWS
endpoint (e.g. `https://pswsnetwork.eng.ua.edu/api/heartbeat`); the
**response body carries directives** (§4). Clean request/response,
standard, easy to evolve. Auth via a token derived from the station's
PSWS registration (or mutual-TLS with the portal key).

**(B) SFTP push + pull — fallback, zero new credentials.** Reuses the
*exact* `PswsMagnetometerSftp` path (board #25): station SFTPs
`heartbeat/<station_id>/<ts>.json` and reads back a per-station
`directives/<station_id>.json`. Polling, not true req/resp, but needs
**no new auth** beyond the key already registered. Good for a v0 that
ships before the server grows an HTTP API.

Either way it slots into the existing architecture as a new `hs-uploader`
transport (`HeartbeatTransport`) fed by a sigmond-core source that
assembles the envelope — the same Source/Transport/Watermark model
(README) the data uploads already use.

**Cadence.** Default every 5 min (`SIGMOND_HEARTBEAT_INTERVAL_SEC`,
configurable). Availability resolution at the server is the cadence;
pick to balance the Gantt view's granularity against load.

## 4. The response (server→station) — optional directives

The response is where request/response earns its keep. All directives
are **optional and advisory**; a station that ignores them still
functions. v0 may return an empty `{}`.

```json
{
  "ack": true,
  "server_time": "2026-06-24T19:42:06Z",
  "requests": [
    {"type": "level0_pull", "target": "wspr",
     "from": "2026-06-24T14:00:00Z", "to": "2026-06-24T15:00:00Z",
     "request_id": "r-8821"}
  ],
  "notices": [
    {"severity": "info", "message": "contract_version 0.8 available"}
  ]
}
```

- **`level0_pull`** is the mechanism behind *Request Level-0 from station
  cache*: the science user's request reaches the station on its next
  heartbeat; the station stages the requested high-res window from the
  sink and ships it (existing upload path) tagged with `request_id`. This
  is exactly the "uploaded with next heartbeat response" the board item
  describes.
- **`notices`** lets the server nudge operators (stale contract, retired
  endpoint) without a separate channel.

## 5. What it unblocks (board traceability)

| Board item | What the heartbeat provides |
|-----------|------------------------------|
| Database query / network health | The `instruments` + `health` + `uploads` roll-up *is* the "who's contributing, how, when, how much" feed. |
| Station-availability / Gantt view | Heartbeat arrival times → the availability timeline, with no extra station work. |
| Per-station timing provenance ([#50](https://github.com/HamSCI/psws-charette/issues/50)) | `timing` block carries tier + offset + source per heartbeat; pairs with the per-row `timing_authority_applied` already in data. |
| Request Level-0 from station cache | The `level0_pull` directive + `request_id` round-trip. |
| Automatic email alerts when station goes down | Missing heartbeats are the down signal the alerting already wants. |

## 6. Versioning & change discipline

- The envelope carries `schema_version` (start at `1`). Any field-shape
  change bumps it; the server declares accepted versions and rejects
  mismatches cleanly (same `stale-schema` halt discipline as the sink).
- New directive `type`s are additive; unknown types are ignored by the
  station (forward-compatible by construction).
- The heartbeat is **not** a `pending_uploads` row — register it as a new
  record kind in `hs-uploader`, not a new `target_db`, so the per-stream
  data path stays unchanged.

## 7. Open questions for the Monday meeting

1. **Endpoint + auth** (UA's call): HTTP POST with a registration-derived
   token, or SFTP push/pull reusing the portal key? (A) is cleaner; (B)
   needs nothing new.
2. **Cadence vs. availability granularity** — 5 min default acceptable
   for the Gantt view, or finer?
3. **Directive scope for v0** — ship health/availability only first, add
   `level0_pull` once the server can originate requests?
4. **Identity authority** — does the heartbeat *also* carry capability
   metadata (instrument list) to seed registration (board #25), or stay
   health-only and leave metadata to the registration handshake?

## 8. Suggested phasing

- **v0 — observe only.** Envelope §2 + transport (B or A), no directives.
  Lights up network-health + availability + timing-provenance
  immediately. Pure additive station work; reuses `smd status`'s existing
  aggregation.
- **v1 — directives.** Add the response channel + `notices`.
- **v2 — Level-0 pull.** Add `level0_pull` once the server can place
  requests; close the loop to the sink cache.
