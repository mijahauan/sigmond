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

from ..coordination import load_coordination, render_env
from ..discovery import usb_sdr
from ..environment import Environment
from ..paths import COORDINATION_ENV, COORDINATION_PATH, SIGMOND_CONF
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
# DFU (firmware-upgrade) bootstrap
# ---------------------------------------------------------------------------

def _is_dfu(sdr) -> bool:
    """True if this discovered SDR appears to be in firmware-upgrade mode.

    Check is a substring match on the sdr_type label produced by
    discovery.usb_sdr — RX-888 advertises product string "RX-888 DFU"
    until radiod uploads firmware and it re-enumerates.
    """
    return "DFU" in (sdr.fields.get("sdr_type") or "").upper()


def _print_detected(sdrs: list) -> None:
    """One-line summary per detected SDR."""
    info(f"detected {len(sdrs)} SDR(s) on USB:")
    for s in sdrs:
        sn = s.fields.get("serial") or "(no readable serial)"
        info(f"  - {s.fields.get('sdr_type')} on bus "
             f"{s.fields.get('bus')}/{s.fields.get('device')}  serial={sn}")


def _bootstrap_dfu_sdrs(dfu_sdrs: list, iface: str) -> bool:
    """Upload firmware to each DFU-mode SDR by briefly running radiod against it.

    Per device:
      1. Write a transient /etc/radio/radiod@bootstrap-fw-N.conf with no
         `serial = ...` line so radiod binds to the first matching DFU
         device on the bus.
      2. systemctl start radiod@bootstrap-fw-N.service — radiod reads the
         conf, opens the SDR, and uploads firmware.
      3. Poll discovery until the DFU count drops (the device has
         re-enumerated as non-DFU) or a timeout expires.
      4. systemctl stop the service and remove the transient conf.

    Multiple DFU devices are bootstrapped sequentially — running two
    transient radiod processes simultaneously would race each other to
    grab whichever DFU SDR enumerates first.

    Returns True if every input device successfully re-enumerated.
    """
    import subprocess
    import time

    BOOTSTRAP_TIMEOUT_S = 30
    POLL_INTERVAL_S     = 1

    success = True
    for i, sdr in enumerate(dfu_sdrs, start=1):
        stub_id   = f"bootstrap-fw-{i}"
        stub_path = RADIOD_CONFIG_DIR / f"radiod@{stub_id}.conf"
        unit      = f"radiod@{stub_id}.service"

        # Map "RX-888 DFU" → "RX-888" so we pick the right frontend profile.
        canonical_type = (sdr.fields.get("sdr_type") or "").replace(" DFU", "").strip()
        profile = _profile_for(canonical_type)

        info(f"  [{i}/{len(dfu_sdrs)}] {sdr.fields.get('sdr_type')} "
             f"on bus {sdr.fields.get('bus')}/{sdr.fields.get('device')}")

        plan = {
            "instance_id":       stub_id,
            "frontend":          profile["section"],
            "status_dns":        f"{stub_id}-status.local",
            "iface":             iface,
            "description":       "bootstrap — transient firmware upload",
            "serial_line":       "# (serial omitted: bind to first matching DFU device)",
            "frontend_defaults": profile["defaults"].rstrip(),
        }
        try:
            stub_path.parent.mkdir(parents=True, exist_ok=True)
            stub_path.write_text(_render(plan))
        except (OSError, PermissionError) as exc:
            err(f"          could not write {stub_path}: {exc}")
            success = False
            continue

        dfu_before = sum(1 for s in _discover_sdrs() if _is_dfu(s))

        r = subprocess.run(["systemctl", "start", unit],
                           capture_output=True, text=True)
        if r.returncode != 0:
            err(f"          systemctl start {unit} failed: "
                f"{(r.stderr or r.stdout).strip() or 'unknown error'}")
            stub_path.unlink(missing_ok=True)
            success = False
            continue
        info(f"          started {unit}; waiting up to "
             f"{BOOTSTRAP_TIMEOUT_S}s for re-enumeration...")

        re_enumerated = False
        deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            if sum(1 for s in _discover_sdrs() if _is_dfu(s)) < dfu_before:
                re_enumerated = True
                ok(f"          firmware loaded — SDR re-enumerated")
                break

        if not re_enumerated:
            err(f"          timeout: SDR did not exit DFU mode within "
                f"{BOOTSTRAP_TIMEOUT_S}s")
            success = False

        # Stop and clean up regardless of outcome — leftover transient
        # services confuse the next run.
        subprocess.run(["systemctl", "stop", unit],
                       capture_output=True, text=True)
        stub_path.unlink(missing_ok=True)

    return success


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

    _print_detected(sdrs)

    # An RX-888 in DFU (Device Firmware Upgrade) mode advertises a
    # placeholder serial (e.g. 0000000004BE) and a "RX-888 DFU" product
    # string; only after radiod uploads the firmware and the device
    # re-enumerates does the real serial show up.  Registering the DFU
    # value would bake a stale sdr_serial into coordination.toml and a
    # wrong instance id (*-rx888dfu) on disk.  Bootstrap firmware now
    # by running radiod transiently, then re-discover and continue with
    # the real serial.
    dfu_sdrs = [s for s in sdrs if _is_dfu(s)]
    if dfu_sdrs:
        print()
        info(f"{len(dfu_sdrs)} SDR(s) in DFU mode — bootstrapping firmware "
             f"upload via a transient radiod@bootstrap-fw-N.service")
        bootstrap_iface = _suggest_iface()
        _bootstrap_dfu_sdrs(dfu_sdrs, bootstrap_iface)
        # Re-discover even if bootstrap reported errors — some devices
        # may have loaded firmware successfully.
        print()
        sdrs = _discover_sdrs()
        info("after firmware load:")
        _print_detected(sdrs)

        still_dfu = [s for s in sdrs if _is_dfu(s)]
        if still_dfu:
            print()
            for s in still_dfu:
                err(f"{s.fields.get('sdr_type')} on bus "
                    f"{s.fields.get('bus')}/{s.fields.get('device')} is "
                    f"still in DFU mode after bootstrap")
            info("Inspect:  journalctl -u 'radiod@bootstrap-fw-*' "
                 "(transient services may have already been removed)")
            return 1

        if not sdrs:
            err("no SDRs detected after firmware bootstrap")
            return 1

    # Build serial→id and id-set views of already-registered radiods so we
    # can (a) skip SDRs whose USB serial is already in coordination.toml
    # and (b) suggest an instance id that doesn't collide with one that
    # already exists.  Without this the wizard would re-prompt for every
    # SDR on every re-run, including the ones the operator named months ago.
    known_serial_to_id: dict[str, str] = {}
    known_ids: set[str] = set()
    try:
        coord = load_coordination(COORDINATION_PATH)
        for r in coord.radiods.values():
            known_ids.add(r.id)
            if r.sdr_serial:
                known_serial_to_id[r.sdr_serial] = r.id
    except (OSError, FileNotFoundError):
        pass

    new_sdrs = []
    for sdr in sdrs:
        serial = (sdr.fields.get("serial") or "").strip()
        if serial and serial in known_serial_to_id:
            ok(f"already registered: {sdr.fields.get('sdr_type')} "
               f"serial {serial} → radiod@{known_serial_to_id[serial]}  "
               f"(skipping)")
            continue
        new_sdrs.append(sdr)

    if not new_sdrs:
        print()
        ok(f"all {len(sdrs)} attached SDR(s) already registered — nothing to do")
        info("To change settings on an existing instance, run:  "
             "smd config edit radiod <id>")
        return 0

    if len(new_sdrs) < len(sdrs):
        print()
        info(f"{len(new_sdrs)} new SDR(s) to register, "
             f"{len(sdrs) - len(new_sdrs)} already known")

    written: list[Path] = []
    coord_blocks: list[str] = []
    iface = _suggest_iface()
    hostname_short = socket.gethostname().split(".")[0]

    for i, sdr in enumerate(new_sdrs):
        print()
        info(f"--- SDR {i + 1}/{len(new_sdrs)} "
             f"({sdr.fields.get('sdr_type')}) ---")
        plan = _collect_per_sdr_values(sdr, args, hostname_short, iface,
                                       known_ids=known_ids)
        if plan is None:
            return 2  # operator aborted
        # Each newly-named id joins known_ids so subsequent SDRs in the same
        # run get a non-colliding default suggestion.
        known_ids.add(plan["instance_id"])
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

    # Re-render /etc/sigmond/coordination.env from the freshly-updated
    # coordination.toml.  Without this, RADIOD_<ID>_STATUS and the
    # contract's SIGMOND_RADIOD_COUNT / SIGMOND_RADIOD_STATUS shortcut
    # would be stale until something else triggered a render — services
    # started before the next render see no value for the new instance.
    try:
        fresh_coord = load_coordination(COORDINATION_PATH)
        env_text = render_env(fresh_coord)
        COORDINATION_ENV.parent.mkdir(parents=True, exist_ok=True)
        COORDINATION_ENV.write_text(env_text)
        ok(f"rendered {COORDINATION_ENV}")
    except (OSError, PermissionError) as exc:
        warn(f"wrote coordination.toml but could not refresh "
             f"{COORDINATION_ENV}: {exc}")

    # Auto-apply each enabled client's [[radiod.fragment]] block to the
    # freshly-created instances (CONTRACT v0.5 §15).  Operators no longer
    # have to remember "now drop psk-recorder/wspr-recorder/... fragments
    # into .conf.d/"; sigmond stages them straight from each client's
    # deploy.toml.  Best-effort — failures degrade to a warning.
    print()
    _apply_fragments_for_new_instances(written)

    print()
    ok(f"radiod ready: wrote {len(written)} config(s)")
    info("Next steps:")
    info("  1. Start radiod:  sudo systemctl enable --now "
         "radiod@<id>.service")
    info("  2. Configure clients:  smd config init <client> [<instance>]")
    return 0


def _apply_fragments_for_new_instances(written: list[Path]) -> None:
    """For each radiod@<id>.conf just written, apply enabled clients'
    [[radiod.fragment]] blocks scoped to that instance.  Quiet when no
    fragments are declared anywhere — that's the common case today."""
    try:
        from .radiod_fragments import apply_fragments
        from ..coordination import load_coordination
        from ..paths import COORDINATION_PATH, TOPOLOGY_PATH
        from ..topology import load_topology
    except ImportError:
        return

    try:
        coord = load_coordination(COORDINATION_PATH)
        topology = load_topology(TOPOLOGY_PATH)
        enabled = topology.enabled_components()
    except (OSError, FileNotFoundError):
        return

    for target in written:
        # target stem is "radiod@<id>"; pull the <id> back out
        rid = target.stem.split('@', 1)[1] if '@' in target.stem else target.stem
        msgs = apply_fragments(coord, list(enabled), radiod_id=rid)
        for msg in msgs:
            stripped = msg.strip()
            if stripped.startswith('warning'):
                warn(stripped)
            else:
                info(stripped)


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
                            iface: str,
                            known_ids: Optional[set] = None) -> Optional[dict]:
    sdr_type = sdr.fields.get("sdr_type", "")
    profile = _profile_for(sdr_type)
    serial = sdr.fields.get("serial", "")

    suggested_id = _default_instance_id(hostname_short, sdr_type, sdr.fields)
    # Bump the suggestion until it doesn't collide with an already-named
    # instance — usb_sdr's `index` distinguishes same-type SDRs on the bus,
    # but it doesn't know about earlier `smd config init radiod` runs.  An
    # operator who plugged in their second RX-888 should be offered
    # `<host>-rx888-2` automatically rather than the colliding default.
    if known_ids:
        base_id = suggested_id
        n = 1
        while suggested_id in known_ids:
            n += 1
            suggested_id = f"{base_id}-{n}"
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
        # Multicast status DNS is, by ka9q-radio convention, the
        # instance id with `-status.local` appended.  Derived silently —
        # there's nothing the operator could meaningfully change here
        # without breaking ka9q-radio's mDNS resolution.
        status_dns = f"{instance_id}-status.local"
        info(f"  multicast status DNS: {status_dns}  "
             f"(derived from instance id)")
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
    # Lock the instance to its physical SDR by USB iSerial when we have
    # one.  Lets a re-run of `smd config init radiod` recognise that
    # this SDR is already registered and skip its prompts; without it,
    # only radiod@<id>.conf carries the binding (and only after parsing).
    serial = (plan.get("serial") or "").strip()
    if serial:
        lines.append(f'sdr_serial  = "{serial}"')
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
