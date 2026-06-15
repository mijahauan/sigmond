# Sigmond install quickstart

A short operator-focused guide to getting `smd` on a fresh Linux host.
Covers the simplified flow from `install.sh`, including the auto-sudo
bootstrap, auto-relocation, and ka9q-python handling.

For deeper background — Proxmox passthrough, networking, host capacity
planning — see [`installation-guide.md`](installation-guide.md).

---

## Prerequisites

- Linux host: Debian 12+, Ubuntu 22.04+, Fedora, RHEL, or similar.
- A shell account that is a member of the `sudo` group (or `wheel`
  on RHEL/Fedora). If you're not, ask root to run:
  ```
  usermod -aG sudo <your-user>
  ```
  then log out and back in. `install.sh` will detect this and exit
  cleanly with these instructions if needed.
- `git` and Python 3.11+ — `install.sh` will install them via your
  distro's package manager if missing.

Hardware (for an actual SDR install, not just `smd`):

- HF antenna
- RX-888 or other ka9q-radio-supported SDR
- GPS-disciplined oscillator (10 MHz + PPS)

---

## What `install.sh` does, in order

1. **Sudo bootstrap.** Detects whether you have passwordless sudo.
   - Already passwordless → continues.
   - Otherwise prompts you once for your password and writes
     `/etc/sudoers.d/sigmond-nopasswd` granting NOPASSWD to your
     user. (To revert later: `sudo rm /etc/sudoers.d/sigmond-nopasswd`.)
   - Not in the sudo group → exits with instructions for root.
2. **Proxmox detection.** Only fires if running in a KVM guest with
   no prior install state and a TTY. Offers to run the host
   passthrough bootstrap first. Always optional.
3. **Canonical-path relocation.** Sigmond's source-of-truth lives at
   `/opt/git/sigmond/sigmond/`. If you ran `install.sh` from
   anywhere else, the script `sudo mv`s your clone there and
   re-execs from the canonical path.
4. **ka9q-python relocation.** Sigmond's `pyproject.toml` declares
   `ka9q-python` as a sibling at `/opt/git/sigmond/ka9q-python`.
   The script searches `~/ka9q-python`, `~/git/ka9q-python`,
   `/opt/git/ka9q-python`; if found, relocates; otherwise clones
   from <https://github.com/mijahauan/ka9q-python>.
5. **System user + group.** Creates the `sigmond` system user/group
   that owns `/opt/git/sigmond/*`. Adds your invoking user to the
   `sigmond` group so you can edit sources as yourself. Sets
   `setgid` on directories so new files inherit the group.
6. **System dirs + config.** Creates FHS dirs (`/etc/sigmond`,
   `/var/lib/sigmond`, `/var/log/sigmond`),
   installs `catalog.toml` and a default disabled-everywhere
   `topology.toml` if not already present.
7. **uv + venv.** Installs `uv` if missing, then builds
   `/opt/git/sigmond/sigmond/venv` and `pip install -e sigmond[tui]`. uv resolves
   the `ka9q-python` path-based dep via `[tool.uv.sources]`.
8. **`smd` symlink.** Symlinks `bin/smd` to `/usr/local/bin/smd`
   (on every user's PATH). Removes any legacy `/usr/local/sbin/smd`.

---

## The simplified flow

```bash
# 1. Clone anywhere you like.  install.sh will move it to the canonical
#    location for you.
git clone https://github.com/mijahauan/sigmond ~/sigmond
cd ~/sigmond

# 2. Run the installer.  Will prompt for password ONCE if passwordless
#    sudo isn't already set up.
./install.sh
```

That's it. After the success banner you have:

- `/usr/local/bin/smd` on your PATH
- `/opt/git/sigmond/{sigmond,ka9q-python}` checked out and owned by
  the `sigmond` system user (group-shared with you)
- `/opt/git/sigmond/sigmond/venv` with sigmond + Textual + Rich + ka9q-python
- `/etc/sigmond/{catalog,topology}.toml`
- `/etc/sudoers.d/sigmond-nopasswd` (if your account didn't already
  have passwordless sudo)

---

## Verifying the install

```bash
which smd                  # → /usr/local/bin/smd
smd list --catalog         # prints the catalog of known clients
ls -la /opt/git/sigmond/   # both repos owned by sigmond:sigmond
```

If `smd` reports import errors, check the venv:

```bash
/opt/git/sigmond/sigmond/venv/bin/python -c "import sigmond, textual, ka9q_python"
```

If `smd` says no permission, confirm your group membership took effect:

```bash
id -nG | tr ' ' '\n' | grep -E 'sudo|sigmond'
```

If you just ran `install.sh` for the first time and added yourself to
the `sigmond` group, log out and back in (or `newgrp sigmond`) for
the new group membership to take effect.

---

## Re-running `install.sh`

`install.sh` is idempotent. Each step checks before mutating
(user already exists? skip useradd. topology.toml already there?
don't overwrite. venv already at canonical location? rebuild it
in place). You can re-run it after a sigmond upgrade or to recover
from a partial install.

If you want to test from scratch:

```bash
sudo systemctl stop sigmond.service          # if running
sudo rm -rf /opt/sigmond /opt/git/sigmond /etc/sigmond /var/lib/sigmond \
           /var/log/sigmond /usr/local/bin/smd /etc/sudoers.d/sigmond-nopasswd
sudo userdel -r sigmond                      # only if no human user shares the name
```

(Watch out for the last command if your invoking shell user *is* named
`sigmond` — a very common case for HamSCI single-purpose appliances.)

---

## Next steps after install

> **For the full operational bring-up** — identity, radiod + RX888 config,
> FFT wisdom, CPU/governor/isolcpus tuning, per-client reporter setup, and
> the validate-driven checklist — follow the
> [greenfield runbook](greenfield-runbook.md). The commands below are just
> the first move.

```bash
smd tui                          # interactive configurator
# or, headless:
smd install radiod
smd install wspr-recorder
sudo vi /etc/sigmond/topology.toml    # set enabled = true
smd start
smd status
```

See the [README](../README.md) for the full command reference and
the per-client config-file inventory.
