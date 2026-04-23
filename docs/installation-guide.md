# Sigmond Installation Guide

Tested on: **Debian 13 (trixie)**, kernel 6.12.74, April 2026.

---

## Overview

Sigmond ("Dr. SigMonD") is the installer and lifecycle manager for the HamSCI
SDR observation suite. The `smd` command coordinates radiod (ka9q-radio),
wspr-recorder, psk-recorder, hf-timestd, and wsprdaemon-client on a shared
SDR receiver.

---

## Hardware Requirements

- HF antenna
- RX-888 or compatible SDR receiver
- GPS-disciplined oscillator (10 MHz + PPS output)
- Linux computer (Debian 12+, Ubuntu 22.04+, or equivalent)

---

## 1. Prepare the System

### 1.1 Ensure sudo access

Sigmond's installer writes to `/etc`, `/opt`, and `/usr/local/sbin`, so you
need sudo. On a fresh Debian install the default user may not be in the sudo
group.

As root:

```bash
su -
usermod -aG sudo <your-username>
```

Then **log out and back in** so the group change takes effect.

> **Tip for automation / non-interactive use:** Configure passwordless sudo so
> that install scripts can run unattended:
>
> ```bash
> su -
> echo "<your-username> ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/<your-username>
> chmod 440 /etc/sudoers.d/<your-username>
> ```

### 1.2 Install system packages

Sigmond's `install.sh` handles git and Python itself, but the following
packages must be present if you plan to build the ka9q-radio C project:

```bash
sudo apt-get update
sudo apt-get install -y git python3-pip python3-venv chrony \
    build-essential cmake
```

| Package | Why |
|---|---|
| `git` | Cloning repos (install.sh checks for it) |
| `python3-pip` / `python3-venv` | Venv fallback if uv is unavailable |
| `chrony` | Required timing daemon for the SDR suite |
| `build-essential` / `cmake` | Compiling ka9q-radio (C project) |

> **Note:** `chrony` will replace `systemd-timesyncd` — this is expected and
> required for the precision timing the SDR suite needs.

---

## 2. Clone the Repositories

Sigmond has a required sibling dependency: **ka9q-python**. Both repos must
live next to each other (the path `../ka9q-python` is hard-coded in
`pyproject.toml`).

```bash
cd ~
git clone https://github.com/mijahauan/sigmond.git
git clone https://github.com/mijahauan/ka9q-python.git
```

The directory layout must look like this:

```
~/
├── sigmond/        ← this repo
└── ka9q-python/    ← sibling dependency
```

> **Bug note (tracked):** `pyproject.toml` uses a hard-coded relative path
> `../ka9q-python` under `[tool.uv.sources]`. If ka9q-python is absent the
> install fails immediately with:
> `error: Distribution not found at: file:///home/<user>/ka9q-python`
> The fix is to clone ka9q-python before running `install.sh`.

---

## 3. Run the Bootstrap Installer

```bash
cd ~/sigmond
bash install.sh
```

The script will:

1. Verify sudo access
2. Confirm git and Python 3.11+ are present (installs them if not)
3. Create FHS directories: `/etc/sigmond/`, `/var/lib/sigmond/`,
   `/var/log/sigmond/`, `/opt/sigmond/`
4. Write `/etc/sigmond/catalog.toml` (component registry)
5. Write `/etc/sigmond/topology.toml` (all components disabled by default)
6. Install `uv` (fast Python package manager) to `/usr/local/bin/`
7. Build `/opt/sigmond/venv` with `sigmond[tui]` (Textual + Rich)
8. Install ka9q-python into the venv (editable)
9. Symlink `bin/smd` → `/usr/local/sbin/smd`

Expected output ends with:

```
[  ok  ] smd installed at /usr/local/sbin/smd
```

> **Bug note (fixed in install.sh):** On a re-run after a failed install,
> `uv venv` refuses to overwrite the existing `/opt/sigmond/venv` directory.
> Fixed by passing `--clear` to `uv venv` (and `--clear` to `python -m venv`
> in the pip fallback path). The script is now safely re-entrant.

---

## 4. Fix PATH

`/usr/local/sbin` is not in the default PATH on Debian. Add it:

```bash
echo 'export PATH="$PATH:/usr/local/sbin"' >> ~/.bashrc
source ~/.bashrc
```

Verify:

```bash
which smd       # should print /usr/local/sbin/smd
smd --help
```

---

## 5. Verify the Installation

```bash
# List all available components from the catalog
sudo smd list --available

# Check service status (all disabled at this point — that's expected)
sudo smd status

# Run diagnostics
sudo smd diag
```

Expected `smd diag` output on a fresh install (no components enabled yet):

```
✓  network: wsprnet.org reachable
⚠  network: graphs.wsprdaemon.org unreachable    ← expected until wsprdaemon-client is running
⚠  network: logs.wsprdaemon.org unreachable      ← expected until wsprdaemon-client is running
   deps.conf not found at /home/wsprdaemon/...   ← expected until components installed
```

The two network warnings and the `deps.conf` notice are normal at this stage.

---

## 6. Configure and Install Components

Edit `/etc/sigmond/topology.toml` to enable the components you want, then
install them. All components are disabled by default.

### Option A — Interactive TUI (recommended)

```bash
sudo smd tui
```

Use the Install screen to browse and enable components visually.

### Option B — Install a specific component

```bash
sudo smd install radiod
sudo smd install wspr-recorder
sudo smd install psk-recorder
sudo smd install hf-timestd
sudo smd install wsprdaemon-client
```

### Option C — Install everything in the catalog

```bash
sudo smd install
```

---

## 7. Topology Reference

`/etc/sigmond/topology.toml` controls what runs on this host. Example with
common components enabled:

```toml
[component.radiod]
enabled = true
managed = true

[component.hf-timestd]
enabled = true

[component.psk-recorder]
enabled = true

[component.wspr-recorder]
enabled = false        # set true if you want WSPR audio capture

[component.wsprdaemon-client]
enabled = true
```

---

## 8. Key Management Commands

| Command | Description |
|---|---|
| `sudo smd start` | Start all enabled components |
| `sudo smd stop` | Stop all enabled components |
| `sudo smd restart` | Restart all enabled components |
| `sudo smd status` | Show service health |
| `sudo smd log <client>` | Follow logs for a client |
| `sudo smd diag` | Run cross-component diagnostics |
| `sudo smd validate` | Check cross-client harmonization rules |
| `sudo smd update` | Pull latest code and re-apply |
| `sudo smd list` | Show configured units |
| `sudo smd list --available` | Show full component catalog |

---

## Bugs Found and Fixed During This Install

| # | Issue | Fix |
|---|---|---|
| 1 | `sigmond` user not in sudoers on fresh Debian 13 | `usermod -aG sudo sigmond` as root |
| 2 | `ka9q-python` sibling repo not cloned before running `install.sh` | Clone `ka9q-python` alongside `sigmond` before running installer |
| 3 | `install.sh` fails on re-run: `uv venv` won't overwrite existing venv | Added `--clear` flag to `uv venv` and `python -m venv` calls in `_venv_create()` |
| 4 | `/usr/local/sbin` not in default PATH on Debian 13 | Add `export PATH="$PATH:/usr/local/sbin"` to `~/.bashrc` |

---

## System State After Successful Install

```
/etc/sigmond/
├── catalog.toml        — component registry (do not edit)
└── topology.toml       — which components are enabled (edit this)

/opt/sigmond/
└── venv/               — Python venv with sigmond[tui] + ka9q-python

/usr/local/sbin/smd     — symlink to ~/sigmond/bin/smd
/usr/local/bin/uv       — fast Python package manager (installed by install.sh)

~/sigmond/              — source repo (smd runs from here)
~/ka9q-python/          — sibling dependency (editable install)
```
