"""`smd admin readiness` — golden-image / site readiness gate."""

from __future__ import annotations

import json
import sys

from .. import readiness
from ..ui import heading, info


_STATUS_EMOJI = {
    "pass": "\033[32m✓\033[0m",
    "warn": "\033[33m⚠\033[0m",
    "fail": "\033[31m✗\033[0m",
    "skip": "\033[90m-\033[0m",
}


def cmd_readiness(args) -> int:
    report = readiness.run_gate(
        getattr(args, 'gate', 'auto') or 'auto',
        profile=getattr(args, 'profile', 'dasi2') or 'dasi2',
        with_optional=getattr(args, 'with_optional', False),
    )

    if getattr(args, 'json', False):
        json.dump(report.as_dict(), sys.stdout, indent=2)
        sys.stdout.write('\n')
        return 0 if report.ready else 1

    heading(f'readiness — {report.gate} gate (profile {report.profile})')
    width = max((len(r.component or '') for r in report.results), default=0)
    for r in report.results:
        sym = _STATUS_EMOJI.get(r.status, '?')
        comp = (r.component or '').ljust(width)
        print(f'  {sym}  {comp}  {r.name}: {r.detail}')
    c = report.counts
    print()
    verdict = ('\033[32mREADY\033[0m' if report.ready
               else '\033[31mNOT READY\033[0m')
    print(f'  \033[1m{verdict}\033[0m  '
          f'\033[32m{c["pass"]} pass\033[0m, '
          f'\033[33m{c["warn"]} warn\033[0m, '
          f'\033[31m{c["fail"]} fail\033[0m, '
          f'\033[90m{c["skip"]} skip\033[0m')
    if report.gate == 'capture' and report.ready:
        info('  image is fit to capture — snapshot/template this VM, then '
             'personalize clones with `smd admin personalize`')
    return 0 if report.ready else 1
