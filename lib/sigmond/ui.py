"""Terminal output helpers and banner.

All human-readable output from these helpers goes to **stderr**, not
stdout.  That way commands like `smd admin validate --json` or `smd config
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


def format_data_path_tag(data_path) -> str:
    """CONTRACT-v0.5 §16.7: short annotation for `smd status` / TUI.

    Returns a `[...]`-formatted tag describing how the instance gets
    its samples, or "" for the default (`radiod-ka9q-python` — implicit
    for v0.4 clients and the dominant case for v0.5).  Output examples:

        ""                      — radiod-ka9q-python (default)
        "[radiod-direct]"       — Path B
        "[kiwisdr]"             — non-radiod client
        "[file:wspr-recorder]"  — meta-client (§16.3.1)
        "[file]"                — replay/test data
        "[other]"               — unknown
    """
    if not isinstance(data_path, dict):
        return ''
    kind = data_path.get('kind')
    if kind in (None, 'radiod-ka9q-python'):
        return ''
    if kind == 'file':
        details = data_path.get('details') or {}
        upstream = details.get('upstream_client')
        return f'[file:{upstream}]' if upstream else '[file]'
    return f'[{kind}]'


# Back-compat aliases so the existing bin/smd keeps working during the
# module extraction.  Remove once all call sites use the bare names.
_ok      = ok
_warn    = warn
_err     = err
_info    = info
_heading = heading
