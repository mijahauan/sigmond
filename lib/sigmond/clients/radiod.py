"""ka9q-radio (radiod) adapter — read-only in Phase 1.

radiod is special: it's not really a "client" of the suite — it's the
server everyone else talks to.  Sigmond reads radiod's own .conf files
to learn sample rate, status DNS, and channel limit for each radiod
instance the coordination config names.
"""

from __future__ import annotations

from pathlib import Path

from ..paths import RADIO_CONF_DIR
from .base import ClientAdapter, ClientView, InstanceView


class RadiodAdapter(ClientAdapter):
    name = "radiod"

    def read_view(self) -> ClientView:
        view = ClientView(client_type="radiod", config_path=RADIO_CONF_DIR)
        if not RADIO_CONF_DIR.exists():
            view.issues.append(f"{RADIO_CONF_DIR} not present")
            return view

        view.installed = True
        for conf in sorted(RADIO_CONF_DIR.glob("radiod@*.conf")):
            instance = conf.stem.split("@", 1)[1]
            view.instances.append(self._read_instance(instance, conf))
        return view

    def _read_instance(self, instance: str, path: Path) -> InstanceView:
        samprate = 0
        status_dns = ""
        try:
            text = path.read_text()
        except OSError:
            return InstanceView(instance=instance)

        # radiod.conf is INI-ish with [section] headers and key = value lines.
        # We want [global] samprate and [global] status (the status DNS).
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1].strip().lower()
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if current == "global":
                if key == "samprate":
                    try:
                        samprate = int(val)
                    except ValueError:
                        pass
                elif key == "status":
                    status_dns = val

        return InstanceView(
            instance=instance,
            radiod_id=instance,
            radiod_samprate_hz=samprate,
            radiod_status_dns=status_dns,
        )
