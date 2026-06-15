"""Whole-host uninstall — tear down a complete sigmond install.

``smd <client> --purge`` (see ``purge.py``) removes ONE client.  This module
removes the *whole* install: every client/library, the non-contract upstream
components (ka9q-radio, ka9q-web — whose ``make install`` footprint we track
ourselves), plus sigmond's own host footprint (host-level systemd units,
cpu-affinity drop-ins incl. orphans, the multicast sysctl, the chrony refclock
drop-in, the grub isolcpus edit, the service users, ``/usr/local/bin``
symlinks, ``/var/lib/sigmond`` data, and the ``/opt/git/sigmond`` checkouts).

Two modes, plus orthogonal toggles, so the plan is always explicit:

  full / bare-metal (default)
      Remove everything: client artifacts, /etc config, /var data, venvs,
      source checkouts, host units + drop-ins, host tunables (sysctl/grub/
      chrony), /usr/local/bin symlinks, and service users.  True clean slate.

  --keep-config
      Preserve the things a reinstall would otherwise force you to redo:
      /etc/<client> + /etc/sigmond + /etc/radio config, /var/lib data, the
      service users, and the /opt/git/sigmond checkouts.  Everything else
      (units, drop-ins, venvs, binaries, /var/log, host tunables) is removed.

Toggles (defaults follow the chosen mode; flags override):
  --keep-data / --wipe-data      keep or remove /var/lib/* (data)
  --keep-host  / --revert-host   keep or revert sysctl/grub/chrony tunables
  --keep-users / --remove-users  keep or remove the service users
  --keep-source/ --remove-source keep or remove /opt/git/sigmond/* checkouts

ka9q-radio / ka9q-web are upstream C projects with no uninstall of their own.
We capture their footprint from their OWN build system — ``make -n install
DESTDIR=<sentinel>`` per subdir (pure dry-run, zero side effects) — and parse
the destinations.  Component-owned dirs (/etc/radio, /var/lib/ka9q-radio,
/usr/local/{lib,share}/ka9q-*) are removed wholesale; individual files in
shared dirs (/usr/local/{bin,sbin}, /etc/systemd/system, udev/sysctl/...) are
removed one by one so the shared dir itself is never touched.

Plan-first ALWAYS.  ``smd admin uninstall`` prints the plan and stops; ``--yes``
executes.  ``--dry-run`` is the explicit no-op form.  Refuses non-root.
"""

from __future__ import annotations

import glob as globmod
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import purge

GIT_BASE = Path("/opt/git/sigmond")
ETC_BASE = Path("/etc")
VAR_LIB = Path("/var/lib")
VAR_LOG = Path("/var/log")
RUN = Path("/run")
DEV_SHM = Path("/dev/shm")
SYSTEMD_SYSTEM = Path("/etc/systemd/system")
USR_LOCAL_BIN = Path("/usr/local/bin")

SYSCTL_FILE = Path("/etc/sysctl.d/99-sigmond-multicast.conf")
CHRONY_REFCLOCKS = Path("/etc/chrony/conf.d/timestd-refclocks.conf")
GRUB_FILE = Path("/etc/default/grub")
GRUB_TOKENS = ("isolcpus", "rcu_nocbs")
CPU_AFFINITY_DROPIN = "smd-cpu-affinity.conf"

# Shared system directories the uninstaller must NEVER remove wholesale, even
# if a client deploy.toml link-step or manifest mistakenly points a dst at one.
# (A deploy.toml with dst=/etc/systemd/system once caused `rmtree` to wipe every
# host service's enable-symlinks.)  Individual files inside these are still
# removable; the directory itself is protected.
_PROTECTED_DIRS = frozenset(Path(x) for x in (
    "/", "/etc", "/etc/systemd", "/etc/systemd/system", "/etc/systemd/user",
    "/etc/udev", "/etc/udev/rules.d", "/etc/sysctl.d", "/etc/cron.d",
    "/etc/modprobe.d", "/etc/logrotate.d", "/etc/sysusers.d", "/etc/default",
    "/etc/chrony", "/etc/chrony/conf.d", "/etc/init.d",
    "/usr", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share",
    "/usr/local", "/usr/local/bin", "/usr/local/sbin", "/usr/local/lib",
    "/usr/local/share", "/usr/local/share/man",
    "/usr/local/share/man/man1", "/usr/local/share/man/man8",
    "/var", "/var/lib", "/var/log", "/var/cache", "/run", "/tmp",
    "/dev", "/dev/shm", "/opt", "/opt/git", "/opt/git/sigmond",
    "/bin", "/sbin", "/lib", "/lib64", "/home", "/root", "/boot",
))

# radio = ka9q-radio's sysusers account; the rest are sigmond/client users.
SERVICE_USERS = ("sigmond", "wsprrec", "pskrec", "timestd", "magrec", "radio")

_LIBRARY_NAMES = frozenset({
    "ka9q-python", "callhash", "hs-uploader", "ft8_lib", "onion",
})

# --- non-contract upstream components: footprint tracked via `make -n install` #
# Map component -> (subdirs to walk).  "." means the repo root Makefile.
_EXTERNAL_COMPONENTS = {
    "ka9q-radio": ["src", "aux", "share", "service", "rules", "docs", "config"],
    "ka9q-web": ["."],
}
# Component-owned dirs removed wholesale, and how each is classified for the
# keep-config / wipe-data toggles.
_KA9Q_COMPONENT_DIRS = {
    "/etc/radio": "config",
    "/var/lib/ka9q-radio": "data",
    "/usr/local/lib/hfdl": "asset",
    "/usr/local/lib/ka9q-radio": "asset",
    "/usr/local/share/ka9q-radio": "asset",
    "/usr/local/share/ka9q-web": "asset",
}
# Shared system dirs: only the individual installed files get removed.
_KA9Q_SHARED_DIRS = frozenset({
    "/usr/local/bin", "/usr/local/sbin",
    "/usr/local/share/man/man1", "/usr/local/share/man/man8",
    "/etc/systemd/system", "/etc/udev/rules.d", "/etc/sysctl.d",
    "/etc/cron.d", "/etc/modprobe.d", "/etc/logrotate.d", "/etc/sysusers.d",
})
# Extra binaries a client built outside its deploy.toml (psk-recorder Phase 1.6).
_EXTRA_BINARIES = (Path("/usr/local/bin/decode_ft8"),)

_FLAG_WITH_VAL = frozenset({"-m", "-o", "-g", "-t"})
_SENTINEL = "/__SIGMOND_UNINSTALL_SENTINEL__"


@dataclass
class UninstallPlan:
    keep_config: bool
    wipe_data: bool
    revert_host: bool
    remove_users: bool
    remove_source: bool
    client_plans: list = field(default_factory=list)
    host_units: list = field(default_factory=list)
    host_unit_files: list = field(default_factory=list)
    dropins: list = field(default_factory=list)
    bin_links: list = field(default_factory=list)
    data_dirs: list = field(default_factory=list)
    log_dirs: list = field(default_factory=list)
    config_dirs: list = field(default_factory=list)
    checkouts: list = field(default_factory=list)
    venvs: list = field(default_factory=list)
    host_files: list = field(default_factory=list)
    grub_revert: bool = False
    users: list = field(default_factory=list)
    ext_files: list = field(default_factory=list)        # individual files, always rm
    ext_asset_dirs: list = field(default_factory=list)   # component asset dirs, always rm


# --------------------------------------------------------------------------- #
# discovery — sigmond clients
# --------------------------------------------------------------------------- #
def _client_names() -> list:
    if not GIT_BASE.is_dir():
        return []
    return sorted(
        p.name for p in GIT_BASE.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "sigmond"
    )


def _deploy_extras(name: str) -> dict:
    repo = GIT_BASE / name
    deploy = purge._read_deploy_toml(repo) if repo.exists() else None
    units: list = []
    link_dsts: list = []
    declared_state: list = []
    if deploy is not None:
        sysd = deploy.get("systemd", {}) or {}
        units += list(sysd.get("units", []))
        units += list(sysd.get("templated_units", []))
        units += list(sysd.get("optional_units", []))
        for step in deploy.get("install", {}).get("steps", []):
            dst = step.get("dst")
            if not dst:
                continue
            dp = Path(dst)
            if step.get("kind") == "link":
                link_dsts.append(dp)
            elif dp.parent in (VAR_LIB, VAR_LOG, RUN, DEV_SHM):
                # Data/log/runtime dirs the deploy.toml actually creates (mkdir/
                # render/install steps) — e.g. hf-timestd's /var/lib/timestd,
                # which the VAR_LIB/<name> convention below MISSES because the
                # dir name (timestd) differs from the component name (hf-timestd).
                # sigmond#11: full uninstall left /var/lib/timestd behind.
                declared_state.append(dp)
    state_dirs = [d / name for d in (VAR_LIB, VAR_LOG, RUN, DEV_SHM)
                  if (d / name).exists()]
    for dp in declared_state:
        if dp.exists() and dp not in state_dirs:
            state_dirs.append(dp)
    return {"units": units, "link_dsts": link_dsts, "state_dirs": state_dirs}


def _bin_links_into_repos() -> list:
    out: list = []
    if not USR_LOCAL_BIN.is_dir():
        return out
    for entry in sorted(USR_LOCAL_BIN.iterdir()):
        try:
            if entry.is_symlink() and str(GIT_BASE) in os.readlink(entry):
                out.append(entry)
        except OSError:
            continue
    return out


def _sigmond_host_units() -> tuple:
    names: list = []
    files: list = []
    if SYSTEMD_SYSTEM.is_dir():
        for f in sorted(SYSTEMD_SYSTEM.iterdir()):
            if f.is_file() and (f.name.startswith("sigmond-")
                                or f.name.startswith("smd-")):
                files.append(f)
                if f.suffix in (".service", ".timer"):
                    names.append(f.name)
    return names, files


def _affinity_dropins() -> list:
    out: list = []
    if not SYSTEMD_SYSTEM.is_dir():
        return out
    for d in sorted(SYSTEMD_SYSTEM.glob("*.service.d")):
        dropin = d / CPU_AFFINITY_DROPIN
        if dropin.exists():
            out.append(dropin)
    return out


def _sweep_orphan_units() -> None:
    """Clean up systemd debris left after unit files are deleted (sigmond#11).

    Name-agnostic and safe: a ``*.wants/<unit>`` enable-symlink whose target
    file we just removed is, by definition, an orphan we created — a broken
    .wants symlink is inert but lingers in ``systemctl`` listings forever.
    Delete those dead links (surviving software's still-valid symlinks resolve
    fine and are left alone), then ``systemctl reset-failed`` so the removed
    units stop showing up as not-found / failed.  Called after the unit files
    are gone and the daemon has been reloaded."""
    if SYSTEMD_SYSTEM.is_dir():
        for wants in SYSTEMD_SYSTEM.glob("*.wants"):
            if not wants.is_dir():
                continue
            try:
                for link in wants.iterdir():
                    # is_symlink() true + exists() false == dangling (target
                    # removed).  Valid links resolve, so exists() stays true.
                    if link.is_symlink() and not link.exists():
                        link.unlink()
            except OSError:
                pass
    subprocess.run(["systemctl", "reset-failed"], capture_output=True, check=False)


def _grub_has_sigmond_tokens() -> bool:
    try:
        text = GRUB_FILE.read_text()
    except OSError:
        return False
    return any(tok + "=" in text for tok in GRUB_TOKENS)


def _existing_users(names: tuple) -> list:
    out: list = []
    for u in names:
        try:
            pwd.getpwnam(u)
            out.append(u)
        except KeyError:
            continue
    return out


# --------------------------------------------------------------------------- #
# discovery — non-contract upstream components (ka9q-radio / ka9q-web)
# --------------------------------------------------------------------------- #
def _split_install_args(toks: list) -> tuple:
    """Return (srcs, tdir) for an `install` argv, skipping flags + flag-values
    (e.g. the mode after -m) and DESTDIR-prefixed dest args."""
    srcs, tdir, i = [], None, 1
    while i < len(toks):
        t = toks[i]
        if t == "-t" and i + 1 < len(toks):
            tdir = toks[i + 1]
            i += 2
            continue
        if t in _FLAG_WITH_VAL:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        if not t.startswith(_SENTINEL):
            srcs.append(t)
        i += 1
    return srcs, tdir


def _make_install_manifest(repo: Path, subdirs: list) -> tuple:
    """Parse `make -n install DESTDIR=<sentinel>` (a side-effect-free dry run)
    for each subdir.  Returns (component_dirs, files): component-owned dirs to
    remove wholesale, and individual files that land in shared system dirs."""
    declared: set = set()
    files: set = set()
    comp: set = set()

    def emit(path: str) -> None:
        for c in _KA9Q_COMPONENT_DIRS:
            if path == c or path.startswith(c + "/"):
                comp.add(c)
                return
        if os.path.dirname(path) in _KA9Q_SHARED_DIRS:
            files.add(path)

    for d in subdirs:
        sub = repo / d if d != "." else repo
        if not (sub / "Makefile").is_file():
            continue
        try:
            out = subprocess.run(
                ["make", "-C", str(sub), "-n", "install",
                 f"DESTDIR={_SENTINEL}"],
                capture_output=True, text=True, check=False, timeout=60,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for raw in out.splitlines():
            line = raw.strip()
            if not line.startswith(("install ", "ln ", "cp ")):
                continue
            try:
                toks = shlex.split(line)
            except ValueError:
                continue
            dest_args = [t for t in toks if t.startswith(_SENTINEL)]
            if not dest_args:
                continue
            if "-d" in toks:
                for dp in dest_args:
                    declared.add(dp[len(_SENTINEL):])
                continue
            srcs, tdir = _split_install_args(toks)
            dest = (tdir[len(_SENTINEL):] if tdir
                    else dest_args[-1][len(_SENTINEL):])
            dest_is_dir = (dest in declared or tdir is not None
                           or dest in _KA9Q_COMPONENT_DIRS
                           or dest in _KA9Q_SHARED_DIRS)
            if not dest_is_dir and len(srcs) <= 1:
                emit(dest)
                continue
            for s in srcs:
                if "*" in s or "?" in s:
                    for m in globmod.glob(str(sub / s)):
                        emit(os.path.join(dest, os.path.basename(m)))
                else:
                    emit(os.path.join(dest, os.path.basename(s)))

    for c in _KA9Q_COMPONENT_DIRS:
        if c in declared or os.path.isdir(c):
            comp.add(c)
    files = {f for f in files
             if not any(f == c or f.startswith(c + "/") for c in comp)}
    return sorted(comp), sorted(files)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def plan_uninstall(*, keep_config: bool, wipe_data: bool, revert_host: bool,
                   remove_users: bool, remove_source: bool) -> UninstallPlan:
    plan = UninstallPlan(
        keep_config=keep_config, wipe_data=wipe_data, revert_host=revert_host,
        remove_users=remove_users, remove_source=remove_source,
    )

    for name in _client_names():
        cp = purge.plan_purge(name)
        extra = _deploy_extras(name)
        merged_units = list(dict.fromkeys(cp["declared_units"] + extra["units"]))
        cp["declared_units"] = merged_units
        cp["expanded_units"] = purge._expand_units(merged_units)
        cp["link_dsts"] = list(dict.fromkeys(cp["link_dsts"] + extra["link_dsts"]))
        cp["state_dirs"] = extra["state_dirs"]
        plan.client_plans.append(cp)
        if cp["venv_dir"]:
            plan.venvs.append(cp["venv_dir"])
        if cp["config_dir"]:
            plan.config_dirs.append(cp["config_dir"])
        if cp["repo_dir"] and name not in _LIBRARY_NAMES:
            plan.checkouts.append(cp["repo_dir"])
        for sd in extra["state_dirs"]:
            if sd.parent == VAR_LIB:
                plan.data_dirs.append(sd)
            elif sd.parent == VAR_LOG:
                plan.log_dirs.append(sd)
    for name in _client_names():
        if name in _LIBRARY_NAMES and (GIT_BASE / name).is_dir():
            plan.checkouts.append(GIT_BASE / name)

    # non-contract upstream components — capture their make-install footprint
    seen_ext: set = set()
    for comp, subdirs in _EXTERNAL_COMPONENTS.items():
        repo = GIT_BASE / comp
        if not repo.is_dir():
            continue
        comp_dirs, files = _make_install_manifest(repo, subdirs)
        for d in comp_dirs:
            cls = _KA9Q_COMPONENT_DIRS.get(d, "asset")
            p = Path(d)
            if cls == "config" and p not in plan.config_dirs:
                plan.config_dirs.append(p)
            elif cls == "data" and p not in plan.data_dirs:
                plan.data_dirs.append(p)
            elif cls == "asset" and p not in plan.ext_asset_dirs:
                plan.ext_asset_dirs.append(p)
        for f in files:
            if f in seen_ext:
                continue
            seen_ext.add(f)
            fp = Path(f)
            plan.ext_files.append(fp)
            # stop+disable any unit we're about to delete
            if fp.parent == SYSTEMD_SYSTEM and fp.suffix in (".service", ".timer"):
                if fp.stem.endswith("@"):       # template e.g. radiod@.service
                    plan.host_units.extend(
                        purge._running_template_instances(fp.name))
                else:
                    plan.host_units.append(fp.name)
    for b in _EXTRA_BINARIES:
        if b.exists() and b not in plan.ext_files:
            plan.ext_files.append(b)

    # RAC (frpc remote-access tunnel) — staged host-side, not a client repo
    rac_unit = SYSTEMD_SYSTEM / "wd-rac.service"
    rac_files = []
    if rac_unit.exists():
        rac_files.append(rac_unit)
        plan.host_units.append("wd-rac.service")
    frpc_bin = Path("/usr/local/sbin/frpc")
    if frpc_bin.exists() and frpc_bin not in plan.ext_files:
        plan.ext_files.append(frpc_bin)

    # sigmond host layer
    host_units, host_files = _sigmond_host_units()
    plan.host_unit_files = host_files + rac_files
    plan.host_units = list(dict.fromkeys(plan.host_units + host_units))
    plan.dropins = _affinity_dropins()
    plan.bin_links = _bin_links_into_repos()
    if (ETC_BASE / "sigmond").exists():
        plan.config_dirs.append(ETC_BASE / "sigmond")
    if (VAR_LIB / "sigmond").exists():
        plan.data_dirs.append(VAR_LIB / "sigmond")
    if (VAR_LOG / "sigmond").exists():
        plan.log_dirs.append(VAR_LOG / "sigmond")
    if (GIT_BASE / "sigmond").is_dir():
        plan.checkouts.append(GIT_BASE / "sigmond")
    if (GIT_BASE / "sigmond" / "venv").is_dir():
        plan.venvs.append(GIT_BASE / "sigmond" / "venv")

    for f in (SYSCTL_FILE, CHRONY_REFCLOCKS):
        if f.exists():
            plan.host_files.append(f)
    plan.grub_revert = _grub_has_sigmond_tokens()
    plan.users = _existing_users(SERVICE_USERS)

    for attr in ("checkouts", "venvs", "config_dirs", "data_dirs", "log_dirs",
                 "ext_asset_dirs", "ext_files"):
        seen, uniq = set(), []
        for p in getattr(plan, attr):
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        setattr(plan, attr, uniq)
    return plan


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def render_plan(plan: UninstallPlan) -> list:
    out: list = []
    mode = "keep-config" if plan.keep_config else "full (bare-metal)"
    out.append(f"sigmond uninstall — mode: {mode}")
    out.append("")

    all_units = []
    for cp in plan.client_plans:
        all_units += cp["expanded_units"]
    all_units += plan.host_units
    all_units = list(dict.fromkeys(all_units))
    if all_units:
        out.append(f"  systemd:   stop+disable {len(all_units)} unit(s)")

    rm = []
    for cp in plan.client_plans:
        for dst in cp["link_dsts"]:
            if dst.is_symlink() or dst.exists():
                rm.append(("unit-link", dst, True))
    for f in plan.host_unit_files:
        rm.append(("host-unit", f, True))
    for d in plan.dropins:
        rm.append(("drop-in", d, True))
    for b in plan.bin_links:
        rm.append(("bin-link", b, True))
    for f in plan.ext_files:
        rm.append(("ext-file", f, True))
    for d in plan.ext_asset_dirs:
        rm.append(("ext-asset", d, True))
    for v in plan.venvs:
        rm.append(("venv", v, True))
    for lg in plan.log_dirs:
        rm.append(("log", lg, True))
    for c in plan.config_dirs:
        rm.append(("config", c, not plan.keep_config))
    for d in plan.data_dirs:
        rm.append(("data", d, plan.wipe_data))
    for c in plan.checkouts:
        rm.append(("source", c, plan.remove_source))
    for f in plan.host_files:
        rm.append(("host-tunable", f, plan.revert_host))

    counts = {}
    for label, path, removed in rm:
        counts.setdefault(label, [0, 0])
        counts[label][0 if removed else 1] += 1
    out.append(f"  files/dirs: {sum(c[0] for c in counts.values())} to remove, "
               f"{sum(c[1] for c in counts.values())} kept")
    for label, path, removed in rm:
        verb = "rm  " if removed else "KEEP"
        out.append(f"  {label:12} {verb} {path}")

    if plan.grub_revert:
        verb = "revert" if plan.revert_host else "KEEP  "
        tail = "(needs reboot)" if plan.revert_host else ""
        out.append(f"  {'grub':12} {verb} {GRUB_FILE} ({'/'.join(GRUB_TOKENS)}) {tail}")
    if plan.users:
        verb = "userdel" if plan.remove_users else "KEEP   "
        out.append(f"  {'users':12} {verb} {', '.join(plan.users)}")

    out.append("")
    if plan.keep_config:
        out.append("  (keep-config: /etc configs preserved for reinstall)")
    return out


# --------------------------------------------------------------------------- #
# execute
# --------------------------------------------------------------------------- #
def _rmtree(path: Path) -> None:
    if path in _PROTECTED_DIRS:
        print(f"  REFUSED — protected system dir, not removed: {path}",
              file=sys.stderr)
        return
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            return
        print(f"  removed {path}")
    except OSError as exc:
        print(f"  warning: rm {path}: {exc}", file=sys.stderr)


def execute_uninstall(plan: UninstallPlan, *, dry_run: bool = False) -> int:
    if dry_run:
        for line in render_plan(plan):
            print(line)
        return 0
    if os.geteuid() != 0:
        print("error: uninstall must run as root", file=sys.stderr)
        return 1

    all_expanded, all_declared = [], []
    for cp in plan.client_plans:
        all_expanded += cp["expanded_units"]
        all_declared += cp["declared_units"]
    all_expanded += plan.host_units
    all_declared += plan.host_units
    for unit in dict.fromkeys(all_expanded):
        subprocess.run(["systemctl", "stop", unit], capture_output=True, check=False)
    for unit in dict.fromkeys(all_declared):
        subprocess.run(["systemctl", "disable", unit], capture_output=True, check=False)

    for cp in plan.client_plans:
        for dst in cp["link_dsts"]:
            if dst.is_symlink() or dst.exists():
                _rmtree(dst)
    for f in plan.host_unit_files:
        _rmtree(f)
    for d in plan.dropins:
        _rmtree(d)
        try:
            if (d.parent.is_dir() and d.parent not in _PROTECTED_DIRS
                    and not any(d.parent.iterdir())):
                d.parent.rmdir()
        except OSError:
            pass
    for b in plan.bin_links:
        _rmtree(b)
    for f in plan.ext_files:
        _rmtree(f)
    for d in plan.ext_asset_dirs:
        _rmtree(d)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)
    _sweep_orphan_units()

    # systemctl stop can return before a daemon exits, and non-conformant units
    # (radiod@) may be missed — so make sure nothing survives unit removal,
    # else userdel later fails with "user is currently used by process".
    for u in _existing_users(SERVICE_USERS):
        subprocess.run(["pkill", "-TERM", "-u", u], capture_output=True, check=False)
    for u in _existing_users(SERVICE_USERS):
        subprocess.run(["pkill", "-KILL", "-u", u], capture_output=True, check=False)

    for v in plan.venvs:
        _rmtree(v)
    for lg in plan.log_dirs:
        _rmtree(lg)

    if not plan.keep_config:
        for c in plan.config_dirs:
            _rmtree(c)
    if plan.wipe_data:
        for d in plan.data_dirs:
            _rmtree(d)
    if plan.remove_source:
        for c in plan.checkouts:
            _rmtree(c)

    if plan.revert_host:
        for f in plan.host_files:
            _rmtree(f)
        subprocess.run(["sysctl", "--system"], capture_output=True, check=False)
        if plan.grub_revert:
            _revert_grub()

    if plan.remove_users:
        for u in plan.users:
            r = subprocess.run(["userdel", u], capture_output=True,
                               text=True, check=False)
            if r.returncode == 0:
                print(f"  userdel {u}")
            else:
                print(f"  warning: userdel {u}: {r.stderr.strip()}", file=sys.stderr)
            # Remove the matching group too — a lingering group (with the
            # operator left as a member) blocks the next install's useradd.
            g = subprocess.run(["getent", "group", u],
                               capture_output=True, text=True, check=False)
            if g.returncode == 0:
                members = g.stdout.strip().split(":")[-1] if ":" in g.stdout else ""
                for m in (x for x in members.split(",") if x):
                    subprocess.run(["gpasswd", "-d", m, u],
                                   capture_output=True, check=False)
                if subprocess.run(["groupdel", u], capture_output=True,
                                  check=False).returncode == 0:
                    print(f"  groupdel {u}")

    return 0


# GRUB cmdline assignments where sigmond's CPU-isolation tokens live, e.g.
#   GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=0,1 rcu_nocbs=0,1"
# Groups: (leading ws)(KEY)(quote char)(inner value)(trailing ws + optional #comment)
_GRUB_CMDLINE_RE = re.compile(
    r'^(\s*)(GRUB_CMDLINE_LINUX(?:_DEFAULT)?)\s*=\s*'
    r'(["\'])(.*)\3(\s*(?:#.*)?)$'
)


def _strip_grub_tokens(inner: str) -> str:
    """Drop sigmond's CPU-isolation tokens from a kernel cmdline VALUE (the text
    already stripped of its surrounding quotes), keeping the remaining words in
    order.  Operates on the unquoted inner content so the closing quote is never
    captured into a token word — the bug behind sigmond#12, where splitting the
    whole assignment let ``rcu_nocbs=0,1"`` (closing quote attached) be removed,
    leaving an unterminated string that breaks ``sh -n`` / grub-mkconfig / apt."""
    kept = [w for w in inner.split()
            if not any(w == tok or w.startswith(tok + "=") for tok in GRUB_TOKENS)]
    return " ".join(kept)


def _revert_grub() -> None:
    try:
        lines = GRUB_FILE.read_text().splitlines()
    except OSError as exc:
        print(f"  warning: read {GRUB_FILE}: {exc}", file=sys.stderr)
        return
    changed = False
    out = []
    for line in lines:
        m = _GRUB_CMDLINE_RE.match(line)
        if m:
            ws, key, quote, inner, tail = m.groups()
            new_inner = _strip_grub_tokens(inner)
            if new_inner != inner:
                line = f"{ws}{key}={quote}{new_inner}{quote}{tail}"
                changed = True
        out.append(line)
    if not changed:
        return
    new_text = "\n".join(out) + "\n"

    # Safety net: never write a grub file that wouldn't parse.  A corrupt
    # /etc/default/grub makes grub-mkconfig fail, which fails the kernel postrm
    # hook, which fails EVERY subsequent apt transaction (sigmond#12).  Validate
    # the rewrite with the same `sh -n` check bringup's preflight uses; if it
    # somehow doesn't parse, leave the working file untouched and warn.
    chk = subprocess.run(["sh", "-n"], input=new_text, text=True,
                         capture_output=True, check=False)
    if chk.returncode != 0:
        detail = (chk.stderr.strip().splitlines() or ["parse failed"])[-1]
        print(f"  warning: skipped grub revert — rewrite would not parse "
              f"({detail}); left {GRUB_FILE} unchanged", file=sys.stderr)
        return

    GRUB_FILE.write_text(new_text)
    print(f"  reverted grub tokens in {GRUB_FILE}")
    r = subprocess.run(["update-grub"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        subprocess.run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"],
                       capture_output=True, check=False)
    print("  update-grub done (reboot to fully clear isolcpus)")
