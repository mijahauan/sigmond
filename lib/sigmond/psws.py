"""Guided PSWS (HamSCI Personal Space Weather Station) upload configuration.

Both hf-timestd and mag-recorder ship their data to the SAME PSWS SFTP server
(pswsnetwork.eng.ua.edu) with the SAME key-based mechanism — they differ only
in the station id, the device/instrument id, and which key file they read.  So
the whole guided flow lives here, once, parameterized by a small per-recorder
field map, and is exposed as:

    smd config <recorder> status     # what's set / missing (read-only)
    smd config <recorder> validate   # live SFTP login test against PSWS
    smd config <recorder> edit       # guided wizard: key -> ids -> validate

`status`/`validate` never touch anything; `edit` is the only mutator.  None of
them is required for the recorder to RUN — the daemon records to its local sink
regardless; this only governs whether the data can be UPLOADED to PSWS.
"""
from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

PSWS_HOST = "pswsnetwork.eng.ua.edu"
PSWS_PORT = 22
PSWS_PORTAL = "https://pswsnetwork.caps.ua.edu/"
_PLACEHOLDER_PREFIX = "<YOUR"


# Per-recorder PSWS field map.  Tuple values are the dotted TOML path to the
# field (section..key).  `user` is the systemd service user the key belongs to
# (keygen + the SFTP test run as that user).
RECORDERS = {
    "hf-timestd": {
        "config":     Path("/etc/hf-timestd/timestd-config.toml"),
        "user":       "timestd",
        "station":    ("station", "id"),
        "instrument": ("station", "instrument_id"),
        "ssh_key":    ("uploader", "sftp", "ssh_key"),
        "default_key": "/home/timestd/.ssh/id_rsa_psws",
        "key_type":   "ed25519",
    },
    "mag-recorder": {
        "config":     Path("/etc/mag-recorder/mag-recorder-config.toml"),
        "user":       "magrec",
        "station":    ("station", "psws_station_id"),
        "instrument": ("station", "instrument_id"),
        "ssh_key":    ("uploader", "ssh_key_file"),
        "default_key": "/etc/hs-uploader/keys/id_ed25519",
        "key_type":   "ed25519",
    },
}


def is_psws_recorder(name: str) -> bool:
    return name in RECORDERS


def is_placeholder(v: object) -> bool:
    s = "" if v is None else str(v).strip()
    return (not s) or s.startswith(_PLACEHOLDER_PREFIX)


# ---- privilege-aware file access (configs are service-user-owned 0640) -------
def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text()
    except PermissionError:
        r = subprocess.run([*_sudo(), "cat", str(p)],
                           capture_output=True, text=True, check=False)
        return r.stdout if r.returncode == 0 else None
    except OSError:
        return None


def _exists(p: Path) -> bool:
    try:
        return p.exists()
    except PermissionError:
        return subprocess.run([*_sudo(), "test", "-e", str(p)],
                              check=False).returncode == 0
    except OSError:
        return False


def _dig(d: dict, path: tuple) -> object:
    cur: object = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


@dataclass
class PswsState:
    recorder: str
    config_exists: bool
    station: str = ""
    instrument: str = ""
    key_path: str = ""
    key_present: bool = False
    issues: list = field(default_factory=list)   # human "what's missing" list

    @property
    def configured(self) -> bool:
        return not self.issues


def read_state(recorder: str) -> PswsState:
    spec = RECORDERS[recorder]
    cfg = spec["config"]
    st = PswsState(recorder=recorder, config_exists=_exists(cfg))
    if not st.config_exists:
        st.issues.append(f"config {cfg} not found (is {recorder} installed?)")
        return st
    text = _read_text(cfg) or ""
    data = tomllib.loads(text) if (tomllib and text) else {}
    station = _dig(data, spec["station"])
    instrument = _dig(data, spec["instrument"])
    key_path = _dig(data, spec["ssh_key"]) or spec["default_key"]
    st.station = "" if is_placeholder(station) else str(station)
    st.instrument = "" if is_placeholder(instrument) else str(instrument)
    st.key_path = str(key_path)
    st.key_present = _exists(Path(st.key_path))
    if not st.station:
        st.issues.append("station id not set")
    if not st.instrument:
        st.issues.append("instrument/device id not set")
    if not st.key_present:
        st.issues.append(f"SSH key missing: {st.key_path}")
    return st


def tcp_reachable(timeout: float = 6.0) -> bool:
    try:
        with socket.create_connection((PSWS_HOST, PSWS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def sftp_login_ok(recorder: str, st: PswsState | None = None) -> tuple[bool, str]:
    """Live test: can the recorder's service user SFTP-login to PSWS as its
    station id with its key?  Returns (ok, detail)."""
    spec = RECORDERS[recorder]
    st = st or read_state(recorder)
    if not st.station:
        return False, "station id not set"
    if not st.key_present:
        return False, f"SSH key not found: {st.key_path}"
    if not tcp_reachable():
        return False, f"cannot reach {PSWS_HOST}:{PSWS_PORT} (network/firewall?)"
    cmd = [*_sudo(), "-u", spec["user"], "sftp",
           "-i", st.key_path,
           "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new",
           f"{st.station}@{PSWS_HOST}"]
    try:
        r = subprocess.run(cmd, input="quit\n", capture_output=True,
                           text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False, f"SFTP login timed out to {PSWS_HOST}"
    except FileNotFoundError:
        return False, "sftp client not installed"
    if r.returncode == 0:
        return True, f"SFTP login OK as {st.station}@{PSWS_HOST}"
    err = (r.stderr or r.stdout or "").strip().splitlines()
    tail = err[-1] if err else "unknown error"
    return False, (f"SFTP login FAILED as {st.station}@{PSWS_HOST} — "
                   f"public key not registered at the portal yet? ({tail[:160]})")


# ---------------------------------------------------------------------------
# Mutation: TOML field writer (preserves comments + owner/mode) + key setup
# ---------------------------------------------------------------------------
import re as _re
import tempfile as _tempfile

_G, _Y, _R, _K, _B, _X = ('\033[32m', '\033[33m', '\033[31m',
                          '\033[90m', '\033[1m', '\033[0m')


def _set_toml_field(text: str, section: str, key: str, value: str) -> str:
    """Set ``[section] key = "value"`` in TOML text, preserving the line's
    leading whitespace and any trailing ``# comment``.  Adds the key (or the
    whole section) if absent.  Quotes the value as a TOML string."""
    quoted = '"%s"' % value.replace('"', '\\"')
    lines = text.splitlines()
    header = f"[{section}]"
    sec_i = next((i for i, ln in enumerate(lines)
                  if ln.strip() == header), None)
    if sec_i is None:
        sep = "" if (not lines or lines[-1].strip() == "") else "\n"
        return text.rstrip("\n") + f"\n{sep}{header}\n{key} = {quoted}\n"
    # scan the section body for the key
    for i in range(sec_i + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            break
        m = _re.match(rf'^(\s*{_re.escape(key)}\s*=\s*)'
                      r'(?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^#\n]*?)'
                      r'(\s*(?:#.*)?)$', lines[i])
        if m:
            lines[i] = f"{m.group(1)}{quoted}{m.group(2)}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    # key absent → insert right after the header
    lines.insert(sec_i + 1, f"{key} = {quoted}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _write_text_preserving(path: Path, text: str) -> None:
    r = subprocess.run([*_sudo(), "stat", "-c", "%U %G %a", str(path)],
                       capture_output=True, text=True, check=False)
    owner = group = mode = None
    if r.returncode == 0 and r.stdout.split():
        owner, group, mode = r.stdout.split()
    with _tempfile.NamedTemporaryFile("w", delete=False, suffix=".toml") as tf:
        tf.write(text)
        tmp = tf.name
    try:
        if owner:
            subprocess.run([*_sudo(), "install", "-m", mode, "-o", owner,
                            "-g", group, tmp, str(path)], check=True)
        else:
            subprocess.run([*_sudo(), "cp", tmp, str(path)], check=True)
    finally:
        os.unlink(tmp)


def _set_fields(recorder: str, updates: list) -> None:
    """updates: list of (section, key, value).  One read-modify-write."""
    spec = RECORDERS[recorder]
    text = _read_text(spec["config"]) or ""
    for section, key, value in updates:
        text = _set_toml_field(text, section, key, value)
    _write_text_preserving(spec["config"], text)


def _gen_key(recorder: str, key_path: str) -> tuple[bool, str]:
    """Generate an ed25519 keypair at key_path as the recorder's service user."""
    spec = RECORDERS[recorder]
    user = spec["user"]
    kp = Path(key_path)
    # ensure the parent .ssh dir exists + correct perms, as the service user
    subprocess.run([*_sudo(), "-u", user, "install", "-d", "-m", "700",
                    str(kp.parent)], check=False)
    r = subprocess.run([*_sudo(), "-u", user, "ssh-keygen", "-t", "ed25519",
                        "-f", key_path, "-N", "", "-C", f"{recorder}-psws"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "ssh-keygen failed").strip()[:200]
    return True, key_path


def _pubkey(key_path: str) -> str:
    r = subprocess.run([*_sudo(), "cat", f"{key_path}.pub"],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# CLI entry points — `smd config <recorder> {status|validate|edit}`
# ---------------------------------------------------------------------------
def installed_unconfigured() -> list:
    """PSWS recorders that are installed (config present) but not finished —
    the set a dasi2 host still needs to complete."""
    out = []
    for rec in RECORDERS:
        st = read_state(rec)
        if st.config_exists and not st.configured:
            out.append((rec, st))
    return out


def print_motd_banner() -> None:
    """Login-time nag (for /etc/update-motd.d): if any installed PSWS recorder
    is unfinished, remind the operator — loudly but briefly.  No network."""
    recs = installed_unconfigured()
    if not recs:
        return
    print(f"\n{_Y}⚠  Sigmond: PSWS upload is not finished{_X} — these record "
          f"locally but will NOT upload until configured:")
    for rec, st in recs:
        print(f"     {_B}{rec}{_X}: {', '.join(st.issues)}")
        print(f"       {_K}→ finish it:{_X} smd config {rec} edit")
    print()


def cmd_status(recorder: str) -> int:
    st = read_state(recorder)
    print(f"{_B}PSWS upload — {recorder}{_X}")
    if not st.config_exists:
        print(f"  {_R}✗{_X} {st.issues[0]}")
        return 1
    def row(label, val, ok):
        mark = f"{_G}✓{_X}" if ok else f"{_Y}⚠{_X}"
        shown = val if val else f"{_Y}(not set){_X}"
        print(f"  {mark} {label:18} {shown}")
    row("station id", st.station, bool(st.station))
    row("instrument/device", st.instrument, bool(st.instrument))
    row("SSH key", f"{st.key_path}" + ("" if st.key_present
        else f"  {_Y}(missing){_X}"), st.key_present)
    if st.configured:
        print(f"  {_G}all PSWS fields present{_X} — run "
              f"`smd config {recorder} validate` for a live login test.")
        return 0
    print(f"  {_Y}incomplete{_X} — run `smd config {recorder} edit` to finish "
          f"(records locally regardless; this only enables PSWS upload).")
    return 1


def cmd_validate(recorder: str) -> int:
    st = read_state(recorder)
    print(f"{_B}PSWS validate — {recorder}{_X}  ({st.station or 'no station id'}"
          f"@{PSWS_HOST})")
    if not st.configured:
        for iss in st.issues:
            print(f"  {_Y}⚠{_X} {iss}")
        print(f"  {_Y}cannot validate until configured{_X} — "
              f"`smd config {recorder} edit`.")
        return 1
    print(f"  … testing SFTP login as {st.station} (this can take a few s)")
    ok, detail = sftp_login_ok(recorder, st)
    if ok:
        print(f"  {_G}✓ {detail}{_X}")
        return 0
    print(f"  {_R}✗ {detail}{_X}")
    print(f"  {_K}if the key was just created, register the public key at "
          f"{PSWS_PORTAL} then re-run validate.{_X}")
    return 1


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"  {label}{suffix}: ").strip()
    except EOFError:
        return default
    return v or default


def cmd_edit(recorder: str) -> int:
    """Guided wizard: SSH key (find / enter / create) → station + device id →
    write → live validate.  Interactive; refuses to run without a TTY."""
    import sys
    spec = RECORDERS[recorder]
    if not read_state(recorder).config_exists:
        print(f"{_R}✗{_X} {recorder} config not found "
              f"({spec['config']}) — install {recorder} first.")
        return 1
    if not sys.stdin.isatty():
        print(f"{_Y}smd config {recorder} edit needs an interactive "
              f"terminal.{_X} Run it directly to finish PSWS setup.")
        return 2

    st = read_state(recorder)
    print(f"{_B}PSWS setup — {recorder}{_X}  (records locally regardless; "
          f"this enables PSWS UPLOAD)\n")

    # 1) SSH key: find / enter / create
    key_path = st.key_path
    if _exists(Path(key_path)):
        print(f"  {_G}✓{_X} SSH key found: {key_path}")
    else:
        print(f"  {_Y}⚠{_X} no SSH key at {key_path}")
        while True:
            choice = _prompt("(c)reate a new key, (e)nter an existing key "
                             "path, or (s)kip", "c").lower()
            if choice.startswith("s"):
                print(f"  {_Y}skipping key — upload stays disabled.{_X}")
                break
            if choice.startswith("e"):
                p = _prompt("path to existing private key")
                if p and _exists(Path(p)):
                    key_path = p
                    print(f"  {_G}✓{_X} using {key_path}")
                    break
                print(f"  {_R}not found:{_X} {p}")
                continue
            # create
            ok, res = _gen_key(recorder, key_path)
            if not ok:
                print(f"  {_R}keygen failed:{_X} {res}")
                p = _prompt("enter an existing key path instead (or blank to "
                            "skip)")
                if p and _exists(Path(p)):
                    key_path = p
                    break
                break
            print(f"  {_G}✓{_X} created {key_path}")
            pub = _pubkey(key_path)
            print(f"\n  {_B}REGISTER THIS PUBLIC KEY{_X} at {_B}{PSWS_PORTAL}{_X} "
                  f"(station {recorder}):\n")
            print(f"    {pub}\n")
            _prompt("press Enter once you've pasted it into the PSWS portal", "")
            break

    # 2) station id + device id
    print()
    station = _prompt("PSWS station id (e.g. S000082)", st.station)
    instrument = _prompt("instrument / device id", st.instrument
                         or ("RM3100" if recorder == "mag-recorder" else ""))

    # 3) write
    updates = [(spec["station"][0], spec["station"][-1], station),
               (spec["instrument"][0], spec["instrument"][-1], instrument)]
    if key_path != st.key_path:
        ssec = ".".join(spec["ssh_key"][:-1])
        updates.append((ssec, spec["ssh_key"][-1], key_path))
    try:
        _set_fields(recorder, updates)
    except Exception as exc:                       # noqa: BLE001
        print(f"  {_R}failed to write config:{_X} {exc}")
        return 1
    print(f"\n  {_G}✓{_X} wrote station={station} instrument={instrument} "
          f"to {spec['config']}")

    # 4) validate
    print()
    rc = cmd_validate(recorder)
    print(f"\n  next: {_B}smd restart {recorder}{_X} to apply, then "
          f"`smd config {recorder} status`.")
    return rc

