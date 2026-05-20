"""Pure helpers for ``smd diag sftp`` (the wsprdaemon-server SFTP probe).

The full command lives in ``bin/smd`` because it shells out to
``ssh-keyscan`` / ``ssh-keygen`` / ``ssh`` and opens raw sockets — both
hard to exercise in unit tests.  This module hosts the *pure* bits the
command needs: parsing the server list, scanning env files for the
service's WD_SFTP_SERVERS / HS_UPLOADER_SSH_KEY_FILE, and the
fingerprint-flap classifier.

Keeping these in a module makes them testable without subprocess
mocking and keeps ``cmd_diag_sftp`` focused on the I/O.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_USER = "wsprdaemon"
DEFAULT_PORT = 22
DEFAULT_KEY_ENV = "HS_UPLOADER_SSH_KEY_FILE"
DEFAULT_SERVERS_ENV = "WD_SFTP_SERVERS"


def parse_server(entry: str, *, default_user: str = DEFAULT_USER,
                 default_port: int = DEFAULT_PORT) -> tuple[str, str, int]:
    """Split a single ``user@host[:port]`` entry into ``(user, host, port)``.

    Either or both of ``user@`` and ``:port`` may be omitted; the
    defaults plug in.  ``host`` is required.  Empty / whitespace-only
    input is rejected with ValueError so the caller can surface a
    config error rather than silently probing an empty target.

    Examples::

      parse_server("wsprdaemon@gw1.wsprdaemon.org:22")
        → ("wsprdaemon", "gw1.wsprdaemon.org", 22)
      parse_server("gw2.wsprdaemon.org")
        → ("wsprdaemon", "gw2.wsprdaemon.org", 22)
      parse_server("gw3:2200")
        → ("wsprdaemon", "gw3", 2200)
    """
    s = entry.strip()
    if not s:
        raise ValueError("empty server entry")
    user = default_user
    if "@" in s:
        user, s = s.split("@", 1)
        if not user.strip():
            raise ValueError(f"empty user in {entry!r}")
        user = user.strip()
    port = default_port
    host = s
    if ":" in host:
        host, port_s = host.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError(
                f"non-integer port {port_s!r} in {entry!r}"
            ) from None
    if not host.strip():
        raise ValueError(f"empty host in {entry!r}")
    return (user, host.strip(), port)


def parse_servers_list(
    servers_csv: str, *,
    default_user: str = DEFAULT_USER,
    default_port: int = DEFAULT_PORT,
) -> list[tuple[str, str, int]]:
    """Parse a comma-separated server list — the env-var shape
    ``WD_SFTP_SERVERS=user@h1,user@h2,...`` and the CLI ``--servers``
    flag share this format.  Empty entries (e.g. trailing commas) are
    skipped silently; malformed entries propagate ValueError so the
    operator gets a clear error rather than a half-parsed list.
    """
    return [
        parse_server(e, default_user=default_user, default_port=default_port)
        for e in servers_csv.split(",")
        if e.strip()
    ]


def _strip_env_quotes(value: str) -> str:
    """Strip a single set of surrounding ``"`` or ``'`` quotes if present.

    Mirrors what shells do for ``KEY="value"`` env files.  Unmatched
    quotes are returned verbatim so a value like ``it's a path`` isn't
    silently mutilated.
    """
    v = value.strip()
    if len(v) >= 2:
        if (v[0] == v[-1]) and v[0] in ('"', "'"):
            return v[1:-1]
    return v


def extract_env_var(text: str, name: str) -> str | None:
    """Extract a single ``NAME=value`` env-file value.

    Returns the first match's value (with surrounding quotes stripped),
    or ``None`` if not found.  Comment lines (``#``) and blanks are
    skipped.  Multiple ``NAME=`` entries — first wins, matching systemd
    EnvironmentFile= semantics for the case operators won't think about
    (later duplicates are dead code).
    """
    prefix = name + "="
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(prefix):
            return _strip_env_quotes(line[len(prefix):])
    return None


def find_env_var(
    env_dir: Path, name: str, *, pattern: str = "*.env",
) -> str | None:
    """Walk ``env_dir`` for files matching ``pattern`` and return the
    first ``name=`` value found.

    The wspr-recorder layout is ``/etc/wspr-recorder/env/*.env`` — one
    file per template instance.  Multi-instance hosts will have
    several; we accept the first match because in practice every
    instance on the same host shares the same gateway list + key path.

    Returns ``None`` if the directory doesn't exist or no file
    contains the variable.
    """
    env_dir = Path(env_dir)
    if not env_dir.is_dir():
        return None
    for f in sorted(env_dir.glob(pattern)):
        try:
            text = f.read_text()
        except OSError:
            continue
        value = extract_env_var(text, name)
        if value:
            return value
    return None


def classify_flapping(
    fps_by_type_per_attempt: Iterable[dict[str, str]],
) -> dict[str, list[str]]:
    """Given a sequence of per-attempt ``{key_type: fingerprint}`` dicts,
    return ``{key_type: [sorted fingerprints]}`` for each key TYPE that
    returned MORE THAN ONE distinct fingerprint across the attempts.

    A server that advertises ED25519 + RSA host keys (the normal case)
    contributes two TYPES with one fingerprint each — not a flap.  A
    flap is a single TYPE returning multiple fingerprints across
    successive scans, which happens when the gateway is fronted by a
    load balancer with mismatched per-backend keys, or when a server-
    side rekey storm is rotating the on-disk key file faster than the
    SSH server can settle.

    Returns an empty dict when nothing is flapping — that's the happy
    path the caller should treat as "stable".
    """
    by_type: dict[str, set[str]] = defaultdict(set)
    for attempt in fps_by_type_per_attempt:
        for key_type, fp in attempt.items():
            by_type[key_type].add(fp)
    return {
        k: sorted(v)
        for k, v in by_type.items()
        if len(v) > 1
    }


def parse_ssh_keygen_l_line(line: str) -> tuple[str, str] | None:
    """Parse one ``ssh-keygen -l -f -`` output line into ``(key_type,
    fingerprint_line)``.

    ssh-keygen prints::

        256 SHA256:abc... gw2.wsprdaemon.org (ED25519)
        3072 SHA256:def... gw2.wsprdaemon.org (RSA)

    The trailing ``(TYPE)`` is what the flap classifier groups by.
    Returns ``None`` if the line doesn't match the expected shape so
    the caller can skip a malformed scan output rather than crash.
    """
    s = line.strip()
    if not s.endswith(")"):
        return None
    paren_open = s.rfind("(")
    if paren_open == -1:
        return None
    key_type = s[paren_open + 1:-1].strip()
    if not key_type:
        return None
    return (key_type, s)
