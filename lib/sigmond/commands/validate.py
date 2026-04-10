"""`smd validate` — runs harmonization rules read-only."""

from __future__ import annotations

import json
import sys

from .. import harmonize
from ..sysview import build_system_view
from ..ui import err, heading, info, ok, warn


_SEVERITY_EMOJI = {
    "pass": "\033[32m✓\033[0m",
    "warn": "\033[33m⚠\033[0m",
    "fail": "\033[31m✗\033[0m",
}


def cmd_validate(args) -> int:
    view = build_system_view()
    results = harmonize.run_all(view)

    if getattr(args, 'json', False):
        payload = {
            "results": [
                {
                    "rule":     r.rule,
                    "severity": r.severity,
                    "message":  r.message,
                    "affected": r.affected,
                }
                for r in results
            ],
            "worst": harmonize.worst_severity(results),
        }
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        heading('validate')
        for r in results:
            sym = _SEVERITY_EMOJI.get(r.severity, "?")
            print(f'  {sym}  {r.rule}: {r.message}')
            if r.affected:
                info(f'    affected: {", ".join(r.affected)}')
        print()
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for r in results:
            counts[r.severity] = counts.get(r.severity, 0) + 1
        print(f'  \033[1msummary\033[0m  '
              f'\033[32m{counts["pass"]} pass\033[0m, '
              f'\033[33m{counts["warn"]} warn\033[0m, '
              f'\033[31m{counts["fail"]} fail\033[0m')

    worst = harmonize.worst_severity(results)
    return 1 if worst == "fail" else 0
