# Sigmond on your network

ka9q-radio distributes audio, IQ, and status as IP multicast (239.x.x.x).
Whether that works across more than one host depends almost entirely on
your switch and whether an **IGMP querier** is present. This page tells
you how to check, and what to do for each case.

---

## The symptom

Multi-host radiod "works for a few minutes, then silently dies." Streams
are fine for ~4–5 minutes after start, then the receiving host stops
seeing packets. Restarting the receiver fixes it — for another 4–5 min.

This is the classic **IGMP-snooping silent failure**:

1. Your managed switch has IGMP snooping *enabled* (it watches IGMP
   Joins to learn which ports want which multicast groups).
2. There is **no IGMP querier** on your segment (no router or
   switch sending periodic General Queries).
3. Without queries, the switch's group memberships time out (~260s on
   typical consumer gear) and it stops forwarding the traffic.

No querier = no refresh = streams die.

---

## Diagnose: `smd diag net`

```
smd diag net              # unprivileged; uses /proc/net/igmp state
sudo smd diag net         # adds a passive raw-socket listen for queries
```

The command classifies your host into one of these environments:

| Classification        | Meaning |
|-----------------------|---------|
| `single-host-safe`    | No external multicast-capable interface, or only overlays (Tailscale, ZeroTier, WireGuard). Keep `ttl=0`. |
| `lan-capable`         | Wired LAN with a querier present. Multi-host radiod should work. |
| `lan-needs-querier`   | Wired LAN, no querier detected. Streams will drop after switch timeout unless you install one. |
| `lan-unsafe`          | Only a Wi-Fi path is multicast-capable. Do not carry sustained multicast over Wi-Fi. |
| `multicast-blocked`   | No multicast-capable interface at all (cloud VPC, locked-down bridge). Multi-host is not supported. |
| `unknown`             | Couldn't decide from unprivileged state alone. Re-run with `sudo`. |

The unprivileged run can tell you a querier is probably present (your
kernel will have downgraded `/proc/net/igmp` to V2, meaning it received
a v2 query recently). The sudo run goes further by passively listening
for a full IGMPv2 query interval (default 130 s) and reporting the
querier's source IP, version, and query interval.

---

## What to do, by classification

### `single-host-safe`

Nothing. Leave `ttl=0` in your radiod configs — that's the safe
default and it pins all multicast traffic to the source host.

### `lan-capable`

You're good. You can set `ttl=1` on radiod instances that need to be
consumed by other hosts on the same segment. Verify your switch has
IGMP snooping *enabled*; with a querier present and snooping on, the
switch prunes the multicast to interested ports only — which is what
you want. Without snooping, multicast becomes broadcast.

### `lan-needs-querier`

You have three options, in order of preference:

1. **Enable the querier on gear you already own.** Many prosumer
   routers and L3 switches can be an IGMP querier — it's sometimes
   buried under "IGMP Proxy", "Multicast Router Port", or "IGMP
   Querier" in the web UI. Check first; this is the cleanest fix.

2. **Buy a managed switch that has a querier.** If your current
   switch snoops but doesn't query, you can add a small managed
   switch (TP-Link Omada, Netgear GS308EP, Mikrotik CRS1xx, etc.) as
   the querier and let snooping work correctly.

3. **Run `igmp-querier`.** A small Python daemon that sends periodic
   IGMP General Queries and does proper querier election (lowest IP
   wins). Install on one reliable host on the segment. Do **not** run
   this on an enterprise/campus LAN — your IT department already has
   a querier, and an unauthorized one can trigger NAC alarms or
   interfere with PIM routing.

Until one of these is in place, keep `ttl=0`.

### `lan-unsafe` (Wi-Fi path)

Don't send data-plane multicast over 802.11. Access points rate-limit
multicast to the slowest associated client (often 1 Mbps on 2.4 GHz),
so a sustained audio stream will eat the airtime for every client on
the SSID. Add a wired path, or stay single-host.

### `multicast-blocked`

You're on a network that drops multicast (cloud VPC, most VPNs,
Docker default bridge). Sigmond multi-host is not supported in this
environment. Run radiod and consumers on the same host with `ttl=0`.

---

## Installing `igmp-querier`

See: https://github.com/mijahauan/igmp-querier

Short version:

```
git clone https://github.com/mijahauan/igmp-querier /opt/git/igmp-querier
sudo /opt/git/igmp-querier/install.sh
sudo systemctl edit igmp-querier        # set IGMP_INTERFACE=eth0
sudo systemctl enable --now igmp-querier
```

Verify it's active from another host on the same segment:

```
sudo smd diag net --listen 130          # should now report a querier
```

Uninstall:

```
sudo /opt/git/igmp-querier/uninstall.sh
```

### When NOT to install igmp-querier

- **Enterprise / campus LANs.** Your IT already has one; running
  another can cause querier-election churn or violate policy.
- **Home networks with an existing L3 router that queries.** Check
  `sudo smd diag net` first; if a querier is reported, don't add
  another.
- **Hosts that reboot often.** Pick a host that stays up — if the
  querier disappears for longer than the snoop timeout (~260 s),
  streams will drop.

---

## TTL: the other half of the story

ka9q-radio's `ttl` setting in `radiod@*.conf` controls how far the
multicast packets travel:

- `ttl = 0` → pinned to the source host. Safe everywhere.
- `ttl = 1` → can leave the host onto one L2 segment. Requires either
  proper IGMP snooping **with a querier**, or a dumb switch you're
  willing to have flood multicast on every port.
- `ttl ≥ 2` → crosses routers. Rarely what you want for ka9q-radio.

**Default to `ttl = 0`.** Only raise it after `smd diag net` returns
`lan-capable` or after you've explicitly installed a querier.

---

## Why this is hard

Multicast is the right abstraction for ka9q-radio — one producer, many
consumers, no per-client overhead. But correct L2 multicast handling
requires a querier (RFC 2236 §8), and consumer networking gear often
ships snooping-on + querier-off, which is the one combination that
silently breaks. Sigmond can't detect and fix your switch for you, but
`smd diag net` will tell you which of the five environments you're in
and what the one correct next step is.
