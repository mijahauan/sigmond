"""`smd config init radiod` and `smd config edit radiod [<instance>]`.

Sigmond owns radiod's initial configuration directly — radiod is the
upstream that all HamSCI clients consume from, not itself a HamSCI
contract client.  The wizard:

  1. Probes the local USB bus for connected SDRs (RX888, AirspyR2,
     AirspyHF+, SDRplay, ...).
  2. For each SDR, prompts the operator for an instance id, a status
     DNS (multicast control address), and an antenna description.
  3. Renders ``/etc/radio/radiod@<id>.conf`` per SDR using
     ``etc/radiod.conf.template``, locking the config to the SDR via
     its ``serial`` (the universal key recognised by every radiod
     frontend driver — see src/{rx888,airspy,airspyhf,sdrplay}.c).
  4. Appends a ``[radiod.<id>]`` block to ``coordination.toml`` so the
     rest of the configurations contract (``SIGMOND_RADIOD_COUNT``,
     ``SIGMOND_RADIOD_INDEX``, ``SIGMOND_RADIOD_STATUS``) immediately
     works for every downstream client.
"""

from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path
from string import Template
from typing import Optional

from ..coordination import load_coordination
from ..discovery import usb_sdr
from ..environment import Environment
from ..paths import COORDINATION_PATH, SIGMOND_CONF
from ..ui import err, heading, info, ok, warn


RADIOD_CONFIG_DIR = Path("/etc/radio")
TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "etc" / "radiod.conf.template"
)


# ---------------------------------------------------------------------------
# Per-frontend defaults
#
# Every entry maps an sdr_type label (as emitted by usb_sdr.probe) to the
# radiod config-section name and a block of vendor-recommended defaults.
# Adding a new front-end is two table entries: section name and defaults.
# ---------------------------------------------------------------------------

_FRONTEND_PROFILES: dict[str, dict] = {
    "RX888": {
        "section": "rx888",
        "defaults": (
            "samprate    = 64800000     # 64.8 Msps; bump to 129600000 only on cool, modern CPUs\n"
            "gainmode    = high\n"
            "# gain      = 1.5            # dB; uncomment to override default\n"
        ),
    },
    "RX-888": {
        "section": "rx888",
        "defaults": (
            "samprate    = 64800000\n"
            "gainmode    = high\n"
        ),
    },
    "RX-888 Mk2": {
        "section": "rx888",
        "defaults": (
            "samprate    = 64800000\n"
            "gainmode    = high\n"
        ),
    },
    "Airspy": {
        "section": "airspy",
        "defaults": (
            "linearity   = yes\n"
            "# lna-agc   = yes\n"
            "# mixer-agc = yes\n"
        ),
    },
    "Airspy HF+": {
        "section": "airspyhf",
        "defaults": (
            "# agc       = yes\n"
        ),
    },
    "SDRplay": {
        "section": "sdrplay",
        "defaults": (
            "# antenna   = \"A\"\n"
        ),
    },
}


def _profile_for(sdr_type: str) -> dict:
    return _FRONTEND_PROFILES.get(sdr_type, {
        "section":  "frontend",
        "defaults": "# fill in front-end-specific defaults\n",
    })


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def cmd_radiod_init(args) -> int:
    heading("config init radiod")
    sdrs = _discover_sdrs()
    if not sdrs:
        err("no recognised SDRs detected on the USB bus")
        info("Plug in an RX888 / Airspy / SDRplay and re-run, or hand-write "
             f"a config under {RADIOD_CONFIG_DIR}/radiod@<id>.conf")
        return 1

    info(f"detected {len(sdrs)} SDR(s) on USB:")
    for s in sdrs:
        sn = s.fields.get("serial") or "(no readable serial)"
        info(f"  - {s.fields.get('sdr_type')} on bus "
             f"{s.fields.get('bus')}/{s.fields.get('device')}  serial={sn}")

    written: list[Path] = []
    coord_blocks: list[str] = []
    iface = _suggest_iface()
    hostname_short = socket.gethostname().split(".")[0]

    for i, sdr in enumerate(sdrs):
        print()
        info(f"--- SDR {i + 1}/{len(sdrs)} "
             f"({sdr.fields.get('sdr_type')}) ---")
        plan = _collect_per_sdr_values(sdr, args, hostname_short, iface)
        if plan is None:
            return 2  # operator aborted
        if _refuse_overwrite(plan["target"], args):
            return 1
        body = _render(plan)
        plan["target"].parent.mkdir(parents=True, exist_ok=True)
        plan["target"].write_text(body)
        ok(f"wrote {plan['target']}")
        (plan["target"].parent / f"{plan['target'].name}.d").mkdir(
            parents=True, exist_ok=True)
        info(f"  channel-fragment dir: "
             f"{plan['target'].parent}/{plan['target'].name}.d/")
        written.append(plan["target"])
        coord_blocks.append(_coord_block(plan))

    print()
    _append_coordination(coord_blocks, args)

    print()
    ok(f"radiod ready: wrote {len(written)} config(s)")
    info("Next steps:")
    info("  1. Drop client channel fragments into the .conf.d/ dirs above")
    info("     (psk-recorder, wspr-recorder, hfdl-recorder, hf-timestd "
         "each install their own).")
    info("  2. Start radiod:  sudo systemctl enable --now "
         "radiod@<id>.service")
    info("  3. Configure clients:  smd config init <client> [<instance>]")
    return 0


def cmd_radiod_edit(args) -> int:
    instance = getattr(args, "instance", None)
    targets = sorted(RADIOD_CONFIG_DIR.glob("radiod@*.conf"))
    if not targets:
        err(f"no radiod configs found under {RADIOD_CONFIG_DIR}")
        info("run `smd config init radiod` first")
        return 1

    if instance:
        candidate = RADIOD_CONFIG_DIR / f"radiod@{instance}.conf"
        if not candidate.exists():
            err(f"{candidate} does not exist; "
                f"available: {', '.join(t.stem.split('@', 1)[1] for t in targets)}")
            return 1
        target = candidate
    elif len(targets) == 1:
        target = targets[0]
    else:
        if getattr(args, "non_interactive", False):
            err(f"multiple radiod configs present; pass <instance>")
            return 1
        target = _pick_target(targets)
        if target is None:
            return 2

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    heading(f"config edit radiod {target.stem.split('@', 1)[1]}")
    info(f"editing: {target}")
    info(f"editor:  {editor}")
    print()
    import subprocess
    try:
        return subprocess.run([editor, str(target)], check=False).returncode
    except (OSError, FileNotFoundError) as e:
        err(f"failed to invoke {editor}: {e}")
        return 1


# ---------------------------------------------------------------------------
# Discovery + interactive collection
# ---------------------------------------------------------------------------

def _discover_sdrs():
    env = Environment()
    return usb_sdr.probe(env, extract_serial=True)


def _suggest_iface() -> str:
    """Pick a default network iface — first non-loopback, non-virtual."""
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name = line.split(":", 1)[0].strip()
                if not name or name == "lo":
                    continue
                if name.startswith(("docker", "br-", "veth", "virbr")):
                    continue
                return name
    except OSError:
        pass
    return "eth0"


def _collect_per_sdr_values(sdr, args, hostname_short: str,
                            iface: str) -> Optional[dict]:
    sdr_type = sdr.fields.get("sdr_type", "")
    profile = _profile_for(sdr_type)
    serial = sdr.fields.get("serial", "")

    suggested_id = _default_instance_id(hostname_short, sdr_type, sdr.fields)
    suggested_status = f"{suggested_id}-status.local"
    suggested_desc = _default_description(sdr_type)

    if getattr(args, "non_interactive", False):
        instance_id = suggested_id
        status_dns = suggested_status
        description = suggested_desc
    else:
        instance_id = _prompt("Instance id (used for "
                              f"radiod@<id>.conf and systemd unit)",
                              suggested_id, required=True)
        if instance_id == "__abort__":
            return None
        default_status = f"{instance_id}-status.local" \
            if instance_id != suggested_id else suggested_status
        status_dns = _prompt("Status multicast DNS",
                             default_status, required=True)
        description = _prompt("Antenna description "
                              "(callsign + antenna; written to "
                              f"[{profile['section']}].description)",
                              suggested_desc, required=True)

    if not serial:
        warn(f"no readable iSerial for this {sdr_type}; the rendered "
             f"config will omit `serial = ...` and radiod will pick the "
             f"first matching device.  Add a udev rule and re-run "
             f"`smd config init radiod` to lock the binding.")
        serial_line = "# serial   = \"<run with udev access for stable binding>\""
    else:
        serial_line = f'serial      = "{_format_serial(sdr_type, serial)}"'

    target = RADIOD_CONFIG_DIR / f"radiod@{instance_id}.conf"

    return {
        "instance_id": instance_id,
        "status_dns":  status_dns,
        "description": description,
        "frontend":    profile["section"],
        "frontend_defaults": profile["defaults"].rstrip(),
        "serial":      serial,
        "serial_line": serial_line,
        "iface":       iface,
        "target":      target,
        "sdr_type":    sdr_type,
    }


def _default_instance_id(host: str, sdr_type: str, fields: dict) -> str:
    short = (sdr_type or "sdr").lower()
    short = short.replace(" ", "").replace("+", "p").replace("-", "")
    n = int(fields.get("index", 0) or 0)
    if n:
        return f"{host}-{short}-{n}"
    return f"{host}-{short}"


def _default_description(sdr_type: str) -> str:
    return f"{sdr_type} (set callsign + antenna)"


def _format_serial(sdr_type: str, serial: str) -> str:
    """Airspy and AirspyHF+ conventions use bare hex (no leading 0x);
    most other vendors print serials as bare strings.  We keep what
    lsusb showed but strip a leading `0x` so the rendered config
    matches the ka9q-radio examples."""
    s = serial
    if s.lower().startswith("0x"):
        s = s[2:]
    return s


def _refuse_overwrite(target: Path, args) -> bool:
    if not target.exists():
        return False
    if getattr(args, "reconfig", False):
        warn(f"{target} exists; --reconfig was passed, overwriting")
        return False
    err(f"{target} already exists.  Pass --reconfig to overwrite, or "
        f"run `smd config edit radiod {target.stem.split('@', 1)[1]}` "
        f"to edit it in place.")
    return True


def _pick_target(targets: list[Path]) -> Optional[Path]:
    print("\nMultiple radiod configs present.  Pick one:")
    for i, t in enumerate(targets, start=1):
        print(f"  {i}) {t.stem.split('@', 1)[1]}   ({t})")
    while True:
        try:
            choice = input(f"Select [1-{len(targets)}] (q to quit): ").strip()
        except EOFError:
            return None
        if choice.lower() == "q":
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(targets):
                return targets[idx]
        except ValueError:
            pass
        print("  invalid choice")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render(plan: dict) -> str:
    tpl = Template(TEMPLATE_PATH.read_text())
    return tpl.substitute(
        INSTANCE_ID=plan["instance_id"],
        FRONTEND=plan["frontend"],
        STATUS_DNS=plan["status_dns"],
        IFACE=plan["iface"],
        DESCRIPTION=plan["description"],
        SERIAL_LINE=plan["serial_line"],
        FRONTEND_DEFAULTS=plan["frontend_defaults"],
    )


# ---------------------------------------------------------------------------
# Coordination registration
# ---------------------------------------------------------------------------

def _coord_block(plan: dict) -> str:
    lines = [
        f'[radiod."{plan["instance_id"]}"]',
        'host        = "localhost"',
        f'status_dns  = "{plan["status_dns"]}"',
        '# samprate_hz = 0           # fill in once a channel fragment lands',
        '# cores       = ""',
        f'radio_conf  = "/etc/radio/radiod@{plan["instance_id"]}.conf"',
    ]
    return "\n".join(lines) + "\n"


def _append_coordination(blocks: list[str], args) -> None:
    if not blocks:
        return
    path = COORDINATION_PATH
    existing = path.read_text() if path.exists() else ""

    # Skip blocks whose [radiod."<id>"] header is already declared.
    new_blocks: list[str] = []
    for blk in blocks:
        header = blk.splitlines()[0]
        if header in existing:
            warn(f"{header} already in {path}; skipping coordination append")
            continue
        new_blocks.append(blk)
    if not new_blocks:
        return

    body = "\n".join(new_blocks)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_text = existing + ("\n" if existing else "") + body

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(new_text)
    except PermissionError:
        warn(f"cannot write {path} (try with sudo); coordination block was:")
        for line in body.splitlines():
            print(f"      {line}")
        return
    ok(f"appended {len(new_blocks)} radiod block(s) to {path}")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str, *, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            return "__abort__"
        result = raw or default
        if result or not required:
            return result
        print("  This field is required.")
