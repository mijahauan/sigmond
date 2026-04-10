"""Terminal output helpers and banner.

All human-readable output from these helpers goes to **stderr**, not
stdout.  That way commands like `smd validate --json` or `smd config
show --json` can emit a clean JSON document on stdout without any
warnings or headings leaking into it.  Scripted consumers pipe stdout;
humans see everything on stderr.
"""

import sys

BANNER = r"""
          .---.
         / o o \     "Zo... ven did your
         \ ._. /      signals first start
          |||||       to propagate?"
         /|||||\             ______
        / ||||| \            \    /
       '  |||||  '            \  /
          (  )                 \/
       ~~smoke~~               |   /
                  -------------'  /
                  ---------------/
                  |            |
     Dr. SigMonD — Signal Monitor Daemon
"""


def ok(msg):   print(f'  \033[32m✓\033[0m  {msg}', file=sys.stderr)
def warn(msg): print(f'  \033[33m⚠\033[0m  {msg}', file=sys.stderr)
def err(msg):  print(f'  \033[31m✗\033[0m  {msg}', file=sys.stderr)
def info(msg): print(f'     {msg}', file=sys.stderr)


def heading(title: str) -> None:
    print(f'\n\033[1m━━━ {title} ━━━\033[0m', file=sys.stderr)


# Back-compat aliases so the existing bin/smd keeps working during the
# module extraction.  Remove once all call sites use the bare names.
_ok      = ok
_warn    = warn
_err     = err
_info    = info
_heading = heading
