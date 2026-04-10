"""Terminal output helpers and banner."""

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


def ok(msg):   print(f'  \033[32m✓\033[0m  {msg}')
def warn(msg): print(f'  \033[33m⚠\033[0m  {msg}')
def err(msg):  print(f'  \033[31m✗\033[0m  {msg}', file=sys.stderr)
def info(msg): print(f'     {msg}')


def heading(title: str) -> None:
    print(f'\n\033[1m━━━ {title} ━━━\033[0m')


# Back-compat aliases so the existing bin/smd keeps working during the
# module extraction.  Remove once all call sites use the bare names.
_ok      = ok
_warn    = warn
_err     = err
_info    = info
_heading = heading
