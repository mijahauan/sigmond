# Sigmond Installation Guide

Tested on: **Debian 13 (trixie)**, kernel 6.12.74, April 2026.

---

## Overview

Sigmond ("Dr. SigMonD") is the installer and lifecycle manager for the HamSCI
SDR observation suite. The `smd` command coordinates radiod (ka9q-radio),
wspr-recorder, psk-recorder, and hf-timestd on a shared SDR receiver.

---

## Hardware Requirements

- HF antenna
- RX-888 or compatible SDR receiver
- GPS-disciplined oscillator (10 MHz + PPS output)
- Linux computer (Debian 12+, Ubuntu 22.04+, or equivalent)

---

## Running under Proxmox VE? Read this first

If your station is a Debian 13 VM running under Proxmox VE, sigmond's
`install.sh` will detect the KVM environment and offer to configure
the Proxmox **host** automatically — PCIe USB-controller passthrough,
vfio binding, CPU isolation, hookscript, and one host reboot. After
the reboot, sigmond resumes itself and finishes the in-VM install.

What you need before starting:
- BIOS configured per [`docs/proxmox/wsprdaemon-proxmox-bios-checklist.md`](proxmox/wsprdaemon-proxmox-bios-checklist.md). Sigmond does not configure BIOS.
- Debian 13 VM created in Proxmox with adequate RAM/disk.
- A non-root user inside the VM with sudo access (see §1.1 below).
- The Proxmox host's name or IP, and one-time use of its **root**
  password (for `ssh-copy-id`). After that, all host commands run
  via SSH key.

Quick start:

```bash
sudo mkdir -p /opt/git/sigmond
sudo chown $USER /opt/git/sigmond
git clone https://github.com/mijahauan/sigmond /opt/git/sigmond/sigmond
cd /opt/git/sigmond/sigmond
./install.sh
# answer "y" to the "Proxmox passthrough setup?" prompt
# enter the Proxmox host name/IP when asked
# enter the host root password once when ssh-copy-id prompts
# wait through the host reboot — VM auto-resumes, install completes
```

The full Proxmox flow is documented in [`docs/proxmox/`](proxmox/) and in
[`tasks/plan-proxmox-vm-bootstrap.md`](../tasks/plan-proxmox-vm-bootstrap.md).
Bare-metal users see no new prompts and can skip to §1 below.

---

## 1. Prepare the System

### 1.1 Ensure sudo access

Sigmond's installer writes to `/etc`, `/opt`, and `/usr/local/bin`, so you
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
    build-essential cmake tzdata-legacy
```

| Package | Why |
|---|---|
| `git` | Cloning repos (install.sh checks for it) |
| `python3-pip` / `python3-venv` | Venv fallback if uv is unavailable |
| `chrony` | Required timing daemon for the SDR suite |
| `build-essential` / `cmake` | Compiling ka9q-radio (C project) |
| `tzdata-legacy` | Provides `/usr/share/zoneinfo/right/` for TAI/UTC conversion. Without it, `radiod` falls back to a hardcoded 1972-era 18-second leap offset (current value is 37s) — causing a 19-second error in TAI-timestamped output. Not installed by default on Debian 12/13. |

> **Note:** `chrony` will replace `systemd-timesyncd` — this is expected and
> required for the precision timing the SDR suite needs.

---

## 2. Clone the Repositories

Sigmond and its sibling dependency **ka9q-python** both live under
`/opt/git/sigmond/`. The relative path `../ka9q-python` referenced in
`pyproject.toml` resolves to `/opt/git/sigmond/ka9q-python` when sigmond
is at the canonical `/opt/git/sigmond/sigmond` location.

```bash
sudo mkdir -p /opt/git/sigmond
sudo chown $USER /opt/git/sigmond
git clone https://github.com/mijahauan/sigmond.git     /opt/git/sigmond/sigmond
git clone https://github.com/mijahauan/ka9q-python.git /opt/git/sigmond/ka9q-python
```

The directory layout must look like this:

```
/opt/git/sigmond/
├── sigmond/        ← this repo
└── ka9q-python/    ← sibling dependency
```

After install.sh runs, the whole tree is owned by the system user
`sigmond` (members of group `sigmond` get write access).

> **Bug note (tracked):** `pyproject.toml` uses a hard-coded relative path
> `../ka9q-python` under `[tool.uv.sources]`. If ka9q-python is absent the
> install fails immediately with:
> `error: Distribution not found at: file:///opt/git/sigmond/ka9q-python`
> The fix is to clone ka9q-python alongside sigmond before running `install.sh`.

---

## 3. Run the Bootstrap Installer

```bash
cd /opt/git/sigmond/sigmond
./install.sh
```

The script will:

1. Verify sudo access
2. Confirm git and Python 3.11+ are present (installs them if not)
3. Create FHS directories: `/etc/sigmond/`, `/var/lib/sigmond/`,
   `/var/log/sigmond/`
4. Write `/etc/sigmond/catalog.toml` (component registry)
5. Write `/etc/sigmond/topology.toml` (all components disabled by default)
6. Install `uv` (fast Python package manager) to `/usr/local/bin/`
7. Build `/opt/git/sigmond/sigmond/venv` with `sigmond[tui]` (Textual + Rich)
8. Install ka9q-python into the venv (editable)
9. Symlink `bin/smd` → `/usr/local/bin/smd` (and remove any legacy
   `/usr/local/sbin/smd`)

Expected output ends with:

```
[  ok  ] smd installed at /usr/local/bin/smd
```

> **Bug note (fixed in install.sh):** On a re-run after a failed install,
> `uv venv` refuses to overwrite the existing `/opt/git/sigmond/sigmond/venv` directory.
> Fixed by passing `--clear` to `uv venv` (and `--clear` to `python -m venv`
> in the pip fallback path). The script is now safely re-entrant.

---

## 4. Verify smd is on PATH

`smd` is symlinked into `/usr/local/bin`, which is on every user's
default PATH. Verify:

```bash
which smd       # should print /usr/local/bin/smd
smd --help
```

---

## 5. Verify the Installation

```bash
# List all available components from the catalog
smd list --catalog

# Check service status (all disabled at this point — that's expected)
smd status

# Run diagnostics
smd admin diag
```

Expected `smd admin diag` output on a fresh install (no components enabled yet):

```
✓  network: wsprnet.org reachable
   deps.conf not found at /home/wsprdaemon/...   ← expected until components installed
```

The `deps.conf` notice is normal at this stage.

---

## 6. Configure and Install Components

Edit `/etc/sigmond/topology.toml` to enable the components you want, then
install them. All components are disabled by default.

### Option A — Interactive TUI (recommended)

```bash
smd tui
```

Use the Install screen to browse and enable components visually.

### Option B — Install a specific component

```bash
smd install radiod
smd install wspr-recorder
smd install psk-recorder
smd install hf-timestd
```

### Option C — Install everything in the catalog

```bash
smd install
```

> **Note on first-run timing:** The first `smd install` run will spend
> 10–30 minutes running `fftwf-wisdom` to precompute FFT plans for all the
> sample rates ka9q-radio supports. This is a one-time operation; subsequent
> `smd install` runs skip it because the wisdom file is cached.

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
```

---

## 8. Key Management Commands

| Command | Description |
|---|---|
| `smd start` | Start all enabled components |
| `smd stop` | Stop all enabled components |
| `smd restart` | Restart all enabled components |
| `smd status` | Show service health |
| `smd admin log <client>` | Follow logs for a client |
| `smd admin diag` | Run cross-component diagnostics |
| `smd admin validate` | Check cross-client harmonization rules |
| `smd list` | Show per-component status (git ref, upstream divergence, version policy) |
| `smd component update [<name>]` | Pull latest code per topology version policy and reapply (was `smd list --update`; root) |
| `smd list --catalog` | Show full component catalog (what could be installed) |

---

## Migrating to `/opt/git/sigmond/` (existing hosts)

Sigmond reserves `/opt/git/sigmond/` as the namespace for the clients it
installs and discovers.  Non-sigmond infrastructure (`ka9q-radio`,
`ka9q-web`, `ka9q-python`, `ka9q-update`) continues to live in the
parent `/opt/git/` directly so that directory remains usable for
unrelated repos.

### Pattern A traversability for symlinked checkouts (mode-700 homedirs)

When a client's checkout is staged in a developer's homedir and a
symlink is placed at `/opt/git/sigmond/<name>/`, the client's
*service user* must be able to traverse the homedir.  Many distros
ship homedirs at mode `700` (read/write/exec only for the owner) —
this blocks any service user other than the homedir owner from
following the symlink to the source tree.  `install.sh` catches this
in the Pattern A traversability check and refuses the install with
a clear error.

The standard Linux convention is mode `701` or `711` for homedirs
that contain content meant to be reachable via explicit paths
(traversal-only, no listing): the owner retains full
read/write/exec, others can `cd` through but cannot `ls`.  Apply it
once per affected homedir:

```bash
sudo chmod o+x /home/wsprdaemon
sudo chmod o+x /home/mjh                 # if hosting symlinked checkouts
```

This is needed today on hosts whose `/opt/git/sigmond/<name>/` is a
symlink into a homedir.  On this dev host that's
`gpsdo-monitor`, `hfdl-recorder`, and (when
staged similarly) `codar-sounder`.  Real-directory checkouts owned
by `root` under `/opt/git/sigmond/` don't need this — only the
symlink-into-homedir layout does.

Hosts installed before the namespace move will have sigmond clients at
`/opt/git/<name>/`.  Move them once with:

```bash
sudo mkdir -p /opt/git/sigmond
for d in psk-recorder wspr-recorder hf-timestd \
         hfdl-recorder gpsdo-monitor igmp-querier; do
  if [ -e /opt/git/$d ] && [ ! -e /opt/git/sigmond/$d ]; then
    sudo mv /opt/git/$d /opt/git/sigmond/$d
  fi
done
```

The clients' systemd units, venv targets, and config paths
(`/etc/<client>/...`) are not affected — only the source-checkout
location moves.  After the move, re-run each enabled client's
installer to refresh symlinks under `/usr/local/bin/` so they point at
the new path:

```bash
for d in /opt/git/sigmond/*/; do
  inst=$(ls "$d/scripts/install.sh" "$d/install.sh" 2>/dev/null | head -1)
  [ -n "$inst" ] && sudo bash "$inst"
done
```

Verify with `smd list --catalog` — every previously-installed
client should still show as installed (the symlinks at
`/usr/local/bin/<client>` now resolve through the new path).

---

## Bugs Found and Fixed During This Install

| # | Issue | Fix |
|---|---|---|
| 1 | `sigmond` user not in sudoers on fresh Debian 13 | `usermod -aG sudo sigmond` as root |
| 2 | `ka9q-python` sibling repo not cloned before running `install.sh` | Clone `ka9q-python` alongside `sigmond` before running installer |
| 3 | `install.sh` fails on re-run: `uv venv` won't overwrite existing venv | Added `--clear` flag to `uv venv` and `python -m venv` calls in `_venv_create()` |
| 4 | `/usr/local/sbin` not in default PATH on Debian 13 | Fixed: `smd` now symlinks into `/usr/local/bin` (on every user's PATH); legacy `/usr/local/sbin/smd` is removed on install |
| 5 | TUI topology table: mouse click doesn't toggle rows | Fixed: use `cursor_type="row"` + `RowSelected` event; `RowHighlighted` fires on arrow-key navigation too |
| 6 | TUI `CellDoesNotExist` crash on second click | Fixed: capture `ColumnKey` from `add_columns()` return value and pass key (not label string) to `update_cell()` |
| 7 | `ka9q-web` and `radiod` enabled=True by default in topology | Fixed `_DEFAULT_COMPONENTS` to start all components as `enabled=False` |
| 8 | `'rac'` vs `'wd-rac'` naming mismatch between topology and catalog | Renamed `'rac'` to `'wd-rac'` in `_DEFAULT_COMPONENTS` |
| 9 | `ka9q-python` not visible in topology/catalog | Added as `kind = "library"` catalog entry with `requires = []` |
| 10 | Cascade-disable broken: shared deps (e.g. `radiod`) not removed when last client disabled | Fixed: iterate `reversed(transitive_requires(...))` to process deepest dependents first, removing them from `enabled_now` before checking shared deps |
| 11 | `ka9q-update` not found, or radiod cloned to wrong directory | `smd install` now passes `/opt/git` as target dir to `install-ka9q.sh` |
| 12 | `wireshark-common` debconf interactive prompt hangs `ka9q-update` apt install | Pre-answer: `echo "wireshark-common wireshark-common/install-setuid boolean false" \| sudo debconf-set-selections`, then set `DEBIAN_FRONTEND=noninteractive` |
| 13 | `ka9q-radio` cloned to sigmond source dir instead of `/opt/git/ka9q-radio` | Fixed in `bin/smd`: pass `/opt/git` as arg to `install-ka9q.sh` |
| 14 | `ka9q-python` treated as "unknown" component during `smd install` | Fixed: `library`/`infra` entries without `install_script` are silently skipped (handled by sigmond venv) |
| 15 | `ka9q-python` reinstalled from deps.conf even though already catalog-managed | Fixed: skip pypi deps.conf entries whose name matches a catalog entry |
| 16 | `ka9q-update/install-ka9q.sh` fails on re-run: `git pull` on detached HEAD | Fixed in `install-ka9q.sh`: check out main/master branch before pulling |
| 17 | `install-ka9q.sh` exits with code 1 when stdin is not a TTY (`read -p` returns EOF with `set -e`) | Fixed: add `\|\| true` to both `read -p` calls in `install-ka9q.sh` |
| 18 | `fftwf-wisdom` takes 10–30 minutes during first `smd install` | Expected behavior; runs once. Second `smd install` is fast because wisdom is cached |

---

## System State After Successful Install

```
/etc/sigmond/
├── catalog.toml        — component registry (do not edit)
└── topology.toml       — which components are enabled (edit this)

/opt/git/sigmond/sigmond/
├── venv/               — Python venv with sigmond[tui] + ka9q-python (prod)
├── .venv/              — Python venv with [tui,dev] extras (dev tooling)
└── ...                 — source

/usr/local/bin/smd      — symlink to the repo's bin/smd
/usr/local/bin/uv       — fast Python package manager (installed by install.sh)

~/sigmond/              — source repo (smd runs from here)
~/ka9q-python/          — sibling dependency (editable install)
```
