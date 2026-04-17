# Sigmond

**Dr. SigMonD** (Signal Monitor Daemon) is the installer, lifecycle manager,
and coordinator for the [HamSCI](https://hamsci.org/) SDR observation suite.

Sigmond manages a family of independent clients that share a
[ka9q-radio](https://github.com/ka9q/ka9q-radio) SDR receiver.  Each client
records a different signal type — WSPR, FT8/FT4, HF time standards — and
sigmond handles the parts that require coordination between them: installation,
service lifecycle, log aggregation, diagnostics, and resource arbitration.

```
          .---.
         / o o \     "Zo... ven did your
         \ ._. /      signals first start
          |||||       to propagate?"
         /|||||\
        / ||||| \
       '  |||||  '
          (  )
       ~~smoke~~

     Dr. SigMonD — Signal Monitor Daemon
```

## What you need

**Hardware:**
- An HF antenna
- An [RX-888](https://github.com/ka9q/ka9q-radio) (or other SDR supported by ka9q-radio)
- A GPS disciplined oscillator (e.g. Leo Bodnar GPSDO) providing 10 MHz + PPS
- A Linux computer (Debian 12+ or Ubuntu 22.04+)

**Software prerequisites:**
- Python 3.11 or later
- git
- systemd
- chrony (for time-standard work)

## Quick start

### 1. Install sigmond

```bash
sudo git clone https://github.com/mijahauan/sigmond /opt/git/sigmond
sudo /opt/git/sigmond/bin/smd install
```

This installs the `smd` command to `/usr/local/sbin/smd` and creates
`/etc/sigmond/`.

### 2. See what's available

```bash
smd list --available
```

```
━━━ catalog — available ━━━

  Servers (1)
    ·  radiod                 ka9q-radio SDR daemon

  Clients (4)
    ·  hf-timestd             HF time-standard analyzer (WWV/WWVH/CHU/BPM)
    ·  psk-recorder           FT4/FT8 spot recorder for PSKReporter
    ·  wspr-recorder          WSPR/FST4W audio recorder (period-aligned WAVs)
    ·  wsprdaemon-client      WSPR decoder + poster + uploader
```

### 3. Install clients

Install whichever clients you need.  Each client's installer handles
everything: creates a service user, Python venv, config template, and
systemd units.

```bash
sudo smd install hf-timestd
sudo smd install psk-recorder
```

Use `--dry-run` to preview what would happen without making changes:

```bash
sudo smd install wspr-recorder --dry-run
```

### 4. Configure

Each client owns its own config file.  After installation, edit the config
for your station:

| Client | Config file |
|--------|-------------|
| radiod | `/etc/radio/*.conf` |
| hf-timestd | `/etc/hf-timestd/timestd-config.toml` |
| psk-recorder | `/etc/psk-recorder/psk-recorder-config.toml` |
| wspr-recorder | `/etc/wspr-recorder/wspr-recorder.toml` |
| wsprdaemon-client | `/etc/wsprdaemon/wsprdaemon.conf` |

Sigmond's own coordination config lives at `/etc/sigmond/topology.toml`.
Copy the example to get started:

```bash
sudo cp /opt/git/sigmond/etc/topology.example.toml /etc/sigmond/topology.toml
sudo vi /etc/sigmond/topology.toml
```

Enable or disable components by setting `enabled = true` or `false`.

### 5. Start services

```bash
sudo smd start
```

This starts all enabled components in the topology.  To start a single
component:

```bash
sudo smd start --components psk-recorder
```

### 6. Check health

```bash
smd status
```

Shows systemd unit state for every managed service, plus inventory data
from each installed client: version, channel count, active frequencies,
and any issues.

## Command reference

| Command | Description |
|---------|-------------|
| `smd install [<client>]` | Install a client from the catalog, or run full-suite install |
| `smd start [--components X]` | Start managed services |
| `smd stop [--components X]` | Stop managed services |
| `smd restart [--components X]` | Restart with reset-failed |
| `smd reload [--components X]` | Reload via SIGHUP or restart |
| `smd status [--components X]` | Service health + client inventory |
| `smd list [--available]` | Configured units, or catalog of known clients |
| `smd log <client>` | Follow systemd journal for a client |
| `smd log <client> --files` | Tail the client's file logs |
| `smd log --level DEBUG <client>` | Set log level + SIGHUP (no restart) |
| `smd diag` | Network, dependencies, and client validation |
| `smd validate` | Cross-client harmonization rules |
| `smd config show` | Dump effective coordination config |
| `smd apply` | Reconcile services with current config |
| `smd update` | Pull latest code and re-apply |

All lifecycle commands (`start`, `stop`, `restart`, `reload`, `status`,
`list`) accept `--components X,Y` to filter to specific components.

## Monitoring

### Live status

```bash
smd status
```

For each installed client, status shows the systemd unit state plus data
from the client's contract inventory: version, git commit, number of
channels, active modes, and any reported issues.

### Log viewing

Follow the systemd journal for a client:

```bash
smd log psk-recorder
```

Tail the client's file logs (spot logs, decode output):

```bash
smd log psk-recorder --files
```

### Runtime log level

Change a client's verbosity without restarting:

```bash
sudo smd log --level DEBUG psk-recorder
```

This writes `PSK_RECORDER_LOG_LEVEL=DEBUG` to `/etc/sigmond/coordination.env`
and sends SIGHUP to the client's systemd units.  The client re-reads the
environment and adjusts its logger.

To set a default level for all clients:

```bash
sudo smd log --level WARNING
```

## Debugging

### Run diagnostics

```bash
smd diag
```

Checks:
- Network reachability (wsprnet.org, wsprdaemon services)
- Dependency versions against pinned commits
- Per-client self-validation (`<client> validate --json`)
- Service health

### Cross-client validation

```bash
smd validate
```

Runs harmonization rules across all enabled clients: CPU core isolation,
frequency coverage vs. radiod sample rate, radiod reference resolution,
and timing chain verification.

## Adding a client after initial setup

```bash
# See what's available
smd list --available

# Install it
sudo smd install wspr-recorder

# Edit its config
sudo vi /etc/wspr-recorder/wspr-recorder.toml

# Enable it in topology
sudo vi /etc/sigmond/topology.toml

# Start it
sudo smd start --components wspr-recorder

# Verify
smd status
```

## Available clients

| Client | What it does | Repo |
|--------|-------------|------|
| **radiod** | ka9q-radio SDR daemon — receives RF and multicasts IQ channels | [ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio) |
| **hf-timestd** | HF time-standard analyzer — extracts clock offsets from WWV/WWVH/CHU/BPM | [mijahauan/hf-timestd](https://github.com/mijahauan/hf-timestd) |
| **psk-recorder** | FT4/FT8 spot recorder — decodes and uploads to PSKReporter | [mijahauan/psk-recorder](https://github.com/mijahauan/psk-recorder) |
| **wspr-recorder** | WSPR/FST4W audio recorder — produces period-aligned WAVs for wsprdaemon-client | [mijahauan/wspr-recorder](https://github.com/mijahauan/wspr-recorder) |
| **wsprdaemon-client** | WSPR decoder + poster + uploader — decodes WAVs and reports to wsprnet.org | [rrobinett/wsprdaemon-client](https://github.com/rrobinett/wsprdaemon-client) |

All clients use [ka9q-python](https://github.com/mijahauan/ka9q-python) to
receive RTP streams from radiod.  Each client runs in its own Python venv
and manages its own systemd services.

## How it works

Sigmond follows a layered architecture:

1. **Catalog** — static registry of known clients (`etc/catalog.toml`).
   Answers "what could be installed on this host?"
2. **Installer** — clones a client repo and delegates to its `install.sh`.
   Each client's installer is authoritative; sigmond never duplicates it.
3. **Topology** — per-host config (`/etc/sigmond/topology.toml`).
   Declares which components are enabled.
4. **Lifecycle** — resolves systemd units from each client's `deploy.toml`,
   expands templated units, discovers instances, and drives
   start/stop/restart/reload.
5. **Logging** — aggregates logs across clients via the client contract's
   `log_paths` and `log_level` fields.
6. **Harmonization** — cross-client validation rules that catch conflicts
   (CPU overlap, multicast collisions, missing dependencies).

Each client conforms to the [HamSCI client contract](docs/CLIENT-CONTRACT.md),
which defines a standard interface: `inventory --json`, `validate --json`,
`deploy.toml`, systemd unit conventions, and logging discipline.

## Project

- **Authors:** Michael Hauan (AC0G), Rob Robinett (AI6VN)
- **License:** TBD
- **Repo:** https://github.com/mijahauan/sigmond
- **Part of:** [HamSCI](https://hamsci.org/) — Ham Radio Science Citizen Investigation
