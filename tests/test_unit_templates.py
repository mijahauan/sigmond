"""Guard for sigmond#17 — systemd unit templates must call `smd admin <verb>`.

Several admin-only verbs (wisdom, storage, timing, ...) moved under the
`smd admin` namespace.  Six unit templates kept calling the bare top-level
form (`smd wisdom plan`, `smd storage trim`, `smd timing reconcile`), which
now fails — so FFT wisdom, storage-trim, and timing-reconcile broke on every
fresh install (only a host's already-installed copies had been hand-patched).

This test scans the repo's shipped unit files and fails if any Exec* line
invokes `smd <verb>` where <verb> is an admin subcommand without the `admin`
prefix, so the regression can't return silently."""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Verbs that live under `smd admin` (mirrors the admin_cmds list in bin/smd).
ADMIN_VERBS = {
    "diag", "validate", "verifier", "wisdom", "storage", "environment",
    "sources", "public-ip", "log", "rac", "timing", "radiod", "instance",
    "uninstall", "completion",
}

# Exec*=...  /usr/local/bin/smd <verb> ...   (optionally already `admin <verb>`)
_EXEC_SMD = re.compile(
    r'^\s*Exec\w+\s*=\s*-?\S*\bsmd\s+(?P<rest>.+)$')


def _unit_files():
    for pat in ("**/*.service", "**/*.timer"):
        for f in REPO.glob(pat):
            if "/venv/" in str(f) or "/.venv/" in str(f):
                continue
            yield f


class TestUnitTemplatesAdminPrefix(unittest.TestCase):
    def test_no_bare_admin_verb_in_exec_lines(self):
        offenders = []
        for f in _unit_files():
            for i, line in enumerate(f.read_text().splitlines(), 1):
                m = _EXEC_SMD.match(line)
                if not m:
                    continue
                toks = m.group("rest").split()
                if not toks:
                    continue
                first = toks[0]
                # `smd admin <verb>` is correct; a bare admin verb is the bug.
                if first in ADMIN_VERBS and first != "admin":
                    offenders.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "unit Exec lines call a bare admin verb (need `smd admin <verb>`):\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
