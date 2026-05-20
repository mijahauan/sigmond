# Phase D — wsprdaemon-server PSK cross-rx merge endpoint

Status: **plan, not implemented.** Client-side Phase D (cuts 1-4) is
shipped; this document specifies the optional server-side work that
completes the three-mode upload story.

## Why this exists

Multi-source psk-recorder (Phase B) drives several radiods from one
process.  When the same FT8/FT4 transmission is decoded by multiple
receivers, the client now has three upload-side options:

| `PSK_DELIVERY_PIPELINES` | Behavior |
|---|---|
| `direct` | Client does the cross-rx dedup (SQL window function, Cut 2) and POSTs one winning row per `(time, tx_call, freq_bucket)` directly to pskreporter.info.  No server involvement. |
| `server-merge` | Client ships every receiver's row to wsprdaemon-server tagged with `rx_source`.  Server is expected to dedup across receivers + forward to pskreporter.info on the client's behalf. |
| `server-raw` | Client ships every receiver's row tagged with `rx_source`.  Server stores per-rx but does NOT post to pskreporter (`forward_to_pskreporter=False` on every row). |

`direct` and `server-raw` are fully functional with what's shipped
today.  **`server-merge` requires this endpoint** to do the dedup +
forwarding.  Without it, `server-merge` behaves like `server-raw` on
the receiving side: the server stores per-rx but doesn't post.

## Input contract — what the client now sends

Each FT8/FT4 cycle that survives the upload pump produces one JSONL
file inside the per-host tar:

```
ft8/<RX_SITE>/<RECEIVER>/<BAND>/<cycle>_ft8.jsonl
ft4/<RX_SITE>/<RECEIVER>/<BAND>/<cycle>_ft4.jsonl
```

Each line is one decoded spot.  Fields guaranteed on rows from
psk-recorder ≥ Phase A (every host that opts into server-merge will
be on this version):

| Field | Type | Source |
|---|---|---|
| `time` | ISO-8601 UTC | cycle start, floored per mode (15 s FT8 / 7.5 s FT4) |
| `mode` | `"ft8"` / `"ft4"` | decoder |
| `frequency` | int Hz | absolute decode freq |
| `tx_call` | str | extracted from message |
| `grid` | str | extracted from message |
| `snr_db` | float \| null | jt9 calibrated dB; null for decode_ft8 |
| `score` | int | jt9 sync confidence or decode_ft8 internal score |
| `dt` | float | time-offset within slot (s) |
| `message` | str | raw decoded message |
| `host_call` / `host_grid` | str | operator's station identity |
| `radiod_id` | str | short radiod identifier (e.g. `bee1`) |
| `processing_version` | str | client version string |
| **`rx_source`** | str | **NEW Phase A** — canonical receiver key `radiod:<status_address>` (e.g. `radiod:bee1-status.local`) |
| **`frequency_bucket_hz`** | int | **NEW Phase D Cut 2** — 100 Hz floor of `frequency` (matches PSKReporter dedup tolerance and absorbs ~1-5 Hz inter-receiver jitter) |
| `forward_to_pskreporter` | bool | per-row hint to the server; see below |

Pre-Phase-A spots (rows from older psk-recorder builds) may have
`null` for `rx_source` / `frequency_bucket_hz`.  The endpoint MUST
tolerate this — fall back to `radiod_id` for the rx key and
`(frequency // 100) * 100` for the bucket.

The tar root also contains a `routing.json` that summarises the
per-receiver `forward_to_pskreporter` intent:

```json
{
  "<RX_SITE>/<RECEIVER>": true,
  "<RX_SITE>/<OTHER_RECEIVER>": false
}
```

The flag is conservatively folded across the batch: if any single
row from a receiver wants `forward=False`, the receiver-level entry
is `false`.  The endpoint uses this as the gate for "should I post
this receiver's rows to PSKReporter?"

## Endpoint behavior

Once the tar is extracted on wsprdaemon-server, the existing PSK
ingest already writes rows into the `psk.spots` ClickHouse table.
The new endpoint runs **after ingest** as a separate forwarder so
ingest stays the same shape as before.

### Cross-rx dedup picker

For each `(cycle, mode, tx_call, frequency_bucket_hz)` group across
all receivers that opted in (`routing.json` → `true`), pick **one
winner** — the row with the highest `score` (ties broken by lower
`dt`, then lexicographic `rx_source` for stability).  Discard the
rest from the forward set.

The dedup partition key is identical to the client's own SQL dedup
in psk-recorder's `HsPskReporterUploader` (Phase D Cut 2):

```python
partition_by = (time, tx_call, frequency_bucket_hz)
order_by_desc = score
```

This guarantees the same winner whether the dedup happens client-
side (`direct` pipeline) or server-side (`server-merge`).  The
client+server agree on the partition key so a host running both
pipelines simultaneously doesn't double-post.

### PSKReporter forward

For each surviving (winner) row:

1. Build the PSKReporter UDP-tlv (or TCP) payload exactly as
   `hs_uploader.transports.pskreporter.PskReporterTcp` does on the
   client.
2. The `decoding_software` tag should identify wsprdaemon-server
   (e.g. `wsprdaemon-server-merge/<version>`) so map operators can
   distinguish server-forwarded spots from direct-uploaded ones.
3. `antenna` defaults to the per-receiver string captured from
   ingest (whatever the client supplied).

### What gets *stored* vs *forwarded*

- **Storage** — every row from every receiver is stored as-is.
  Diversity is the whole point of the `server-raw` path; the merge
  endpoint does NOT delete rows after picking a winner.
- **Forwarding** — only the winners are forwarded.  Losers are
  marked-forwarded internally (a new bool column `forwarded_at` or
  similar) so a re-run of the forwarder doesn't double-post.

## Trigger / cadence

Three reasonable shapes:

1. **Cycle-aligned cron** — runs every 15 s (FT8) / 7.5 s (FT4)
   and dispatches all complete cycles older than a settle window
   (≥ 30 s, generous).  Simple, but easy to slip under load.
2. **Ingest hook** — wsprdaemon-server's PSK ingest fires the
   forwarder after each tar is fully written to ClickHouse.
   Tighter latency, but couples ingest + forward.
3. **ClickHouse materialized view / scheduled query** — define a
   view that surfaces per-cycle winners and a scheduled `INSERT
   INTO psk_forwards SELECT ... FROM view` that the forwarder
   reads.  Best fit for wsprdaemon-server's current architecture
   (everything else is ClickHouse-driven).

Recommendation: **option 3**.  Leverages existing infra, keeps the
forwarder stateless (the materialized view is the truth), and the
settle window becomes a simple `WHERE cycle_end_ts < now() - 30s`
predicate.

## Idempotency

A retry of the same tar (e.g. SFTP re-upload after a transient
error) must not double-forward.  Two guards:

1. The forwarder's "winner-picker" view returns the same winner
   deterministically — same partition + ordering = same row.
2. The forwarder marks `forwarded_at` on every row it submits.  On
   re-run, rows with `forwarded_at IS NOT NULL` are skipped.

## Compatibility with the existing path

The current pre-Phase-D behavior (`PSK_DELIVERY_MODE=server` →
`PSK_DELIVERY_PIPELINES=server-merge` via legacy translation) is
that the gw1-elected `pskreporter_forwarder` posts every spot
straight from ClickHouse, no cross-rx dedup.  Hosts on this path
that *don't* have multi-rx (a single radiod_id) still produce one
spot per cycle/band/callsign — the new dedup is a no-op.

Multi-rx hosts on the legacy path currently send the same TX once
per receiver to PSKReporter, and PSKReporter rejects the
duplicates server-side.  This wastes bandwidth and shows up in the
forwarder's reject-rate metrics.  The dedup endpoint fixes it.

## Roll-out

1. Ship the endpoint behind a feature flag (e.g.
   `WSPRDAEMON_SERVER_PSK_MERGE=1`).
2. Enable on one gateway (e.g. gw2) first; compare its PSKReporter
   forward count to gw1's for a representative multi-rx host
   (B4-100 has 3 receivers running today).  Expect ~2-3× drop in
   forwarded volume with no loss of unique spots.
3. Promote to gw1 once the count delta matches expectation.
4. Operators with multi-rx hosts who want the merged behavior set
   `PSK_DELIVERY_PIPELINES=server-merge` (or leave the legacy
   `PSK_DELIVERY_MODE=server` which translates to the same thing).

## Tests

- **Unit (server)** — winner-picker with synthetic ClickHouse rows
  for the same TX heard by 3 receivers at slightly different
  frequencies; assert highest-score row is picked, tied-score rows
  fall back to lowest-dt, then lex `rx_source`.
- **Unit (server)** — `forwarded_at` idempotency: forward,
  re-trigger, assert second pass forwards zero.
- **Unit (server)** — pre-Phase-A row fallback: `rx_source IS NULL`,
  `frequency_bucket_hz IS NULL`, assert the picker derives both
  from `radiod_id` + `frequency` and still produces a deterministic
  winner.
- **Integration** — feed a real psk-recorder tar from a 3-rx host
  into the endpoint, assert one winner per cycle/band/callsign
  reaches PSKReporter (mock the TCP send).

## Cross-references

Client-side cuts that produced this contract:

- psk-recorder `feat/phase-a-rx-source-plumbing` — adds `rx_source`
- psk-recorder `feat/phase-d-cut2-cross-rx-dedup` — adds `frequency_bucket_hz` + the SQL partition key the server should mirror
- psk-recorder `feat/phase-d-cut3-delivery-pipelines` — operator-facing knob
- wsprdaemon-recorder `feat/psk-tar-include-rx-source` — passes the new fields through to the tar JSONL

## Out of scope

- Changes to the tar wire format itself (the new fields are additive JSON keys; legacy consumers ignore them).
- Changes to ClickHouse schema beyond adding the two columns + `forwarded_at` tracking column.
- Renaming the `pskreporter_forwarder` service — its responsibility just narrows from "forward everything" to "forward winners only".
