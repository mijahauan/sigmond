"""RAC (frpc) activation config — render + read helpers.

The sigmond-rac model: the WD admin assigns each station a ``user``, a
``token``, and unique ``remotePort``(s) on gw2.  ``sigmond-rac/install.sh``
renders ``/etc/sigmond/frpc.toml.template`` with the station's proxy names;
ACTIVATION fills the admin-assigned values into that template and writes
``/etc/sigmond/frpc.toml``.  These helpers do the fill/read as pure text
transforms (preserving the template's comments and everything else
verbatim) so the TUI RAC screen and any future CLI share one
implementation.  Fleet constants (serverAddr gw2:35736, TLS CA) live in
the template — never here.
"""

from __future__ import annotations

import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

FRPC_TEMPLATE = '/etc/sigmond/frpc.toml.template'
FRPC_CONFIG = '/etc/sigmond/frpc.toml'


def render_frpc_config(template_text: str, *, user: str, token: str,
                       ssh_port: int, web_port: int = 0) -> str:
    """Fill the admin-assigned values into a rendered frpc template.

    Replaces the ``user`` and ``token`` values and each proxy's
    ``remotePort`` — the ``-WEB`` proxy gets ``web_port``, every other
    proxy ``ssh_port``.  When ``web_port`` is 0 (no web assignment from
    the admin) the whole ``-WEB`` proxy block is dropped: frps rejects
    a remotePort-0 proxy at registration, so shipping it would break
    the SSH tunnel too.  All comments and fleet constants in the
    template pass through verbatim.
    """
    text = template_text

    text = re.sub(r'(?m)^(user\s*=\s*)"[^"]*"', rf'\g<1>"{user}"', text)
    text = re.sub(r'(?m)^(token\s*=\s*)"[^"]*"', rf'\g<1>"{token}"', text)

    # Walk [[proxies]] blocks; set each remotePort by proxy-name suffix.
    lines = text.splitlines()
    out: list[str] = []
    block: list[str] = []
    in_proxy = False

    def _flush() -> None:
        nonlocal block
        if not block:
            return
        name_m = None
        for ln in block:
            name_m = name_m or re.match(r'\s*name\s*=\s*"([^"]*)"', ln)
        is_web = bool(name_m and name_m.group(1).endswith('-WEB'))
        port = web_port if is_web else ssh_port
        if is_web and not web_port:
            block = []          # drop the whole -WEB proxy block
            return
        rewritten = []
        for ln in block:
            m = re.match(r'(\s*remotePort\s*=\s*)\S+(.*)$', ln)
            if m:
                # Drop the "<..._FROM_WD_ADMIN>" placeholder comment once
                # a real port is in place; keep any other trailing text.
                trail = m.group(2)
                if 'FROM_WD_ADMIN' in trail:
                    trail = ''
                ln = f'{m.group(1)}{port}{trail}'
            rewritten.append(ln)
        out.extend(rewritten)
        block = []

    for ln in lines:
        if ln.strip() == '[[proxies]]':
            _flush()
            in_proxy = True
            block = [ln]
            continue
        if in_proxy and ln.strip().startswith('[') \
                and ln.strip() != '[[proxies]]':
            _flush()
            in_proxy = False
            out.append(ln)
            continue
        (block if in_proxy else out).append(ln)
    _flush()

    result = '\n'.join(out)
    if text.endswith('\n') and not result.endswith('\n'):
        result += '\n'
    # Collapse any doubled blank lines a dropped block left behind.
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def read_frpc_values(text: str) -> dict:
    """Extract the operator-relevant values from an frpc.toml for
    pre-filling the activation form.  Returns keys ``user``, ``token``,
    ``ssh_port``, ``web_port``, ``proxy`` (the SSH proxy name); missing
    values are '' / 0."""
    out = {'user': '', 'token': '', 'ssh_port': 0, 'web_port': 0,
           'proxy': ''}
    if tomllib is None:
        return out
    try:
        data = tomllib.loads(text)
    except Exception:
        return out
    out['user'] = str(data.get('user', '') or '')
    out['token'] = str((data.get('auth', {}) or {}).get('token', '') or '')
    for proxy in data.get('proxies', []) or []:
        name = str(proxy.get('name', '') or '')
        try:
            port = int(proxy.get('remotePort', 0) or 0)
        except (TypeError, ValueError):
            port = 0
        if name.endswith('-WEB'):
            out['web_port'] = port
        else:
            out['ssh_port'] = port
            out['proxy'] = name
    return out


def placeholder(value: str) -> bool:
    """True when a template value is still the unfilled <...> marker."""
    v = (value or '').strip()
    return (not v) or (v.startswith('<') and v.endswith('>'))
