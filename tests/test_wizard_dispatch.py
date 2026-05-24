"""Cover sigmond.wizard_dispatch's Tier-1 helpers.

DRAFT: not consumed by any client yet (see
lib/sigmond/wizard_dispatch/README.md), but the API surface is
worth pinning before any client is refactored to depend on it.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from sigmond.wizard_dispatch import (
    SIGMOND_WIZARD_DISPATCH_API,
    is_wizard_available,
    exec_wizard,
    WizardResult,
)


# ---------- is_wizard_available --------------------------------------------

def _ns(**kw) -> argparse.Namespace:
    base = {"non_interactive": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _make_wizard_script(tmp_path: Path, body: str = "#!/bin/bash\nexit 0\n") -> Path:
    p = tmp_path / "wiz.sh"
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_api_version_pinned() -> None:
    """If we ever bump the API, downstream version-pinning consumers
    will see a value-mismatch failure -- bumping is fine, but should
    be deliberate, so guard the constant with a test."""
    assert SIGMOND_WIZARD_DISPATCH_API == "1"


def test_unavailable_when_non_interactive(tmp_path: Path) -> None:
    """--non-interactive always wins, even if everything else is set up."""
    wiz = _make_wizard_script(tmp_path)
    assert is_wizard_available(_ns(non_interactive=True), wiz) is False


def test_unavailable_without_tty(tmp_path: Path) -> None:
    """In pytest stdout isn't a TTY; the wizard must not be invoked."""
    wiz = _make_wizard_script(tmp_path)
    # The function itself checks sys.stdout.isatty() directly; in the
    # pytest context that's False.
    assert is_wizard_available(_ns(), wiz) is False


def test_unavailable_when_script_missing(tmp_path: Path) -> None:
    """Wizard script doesn't exist -- gate down."""
    assert is_wizard_available(_ns(), tmp_path / "nope.sh") is False


def test_unavailable_when_script_not_executable(tmp_path: Path) -> None:
    """File exists but isn't executable -- gate down (dev forgot chmod)."""
    p = tmp_path / "wiz.sh"
    p.write_text("#!/bin/bash\nexit 0\n")
    # NO chmod +x
    assert is_wizard_available(_ns(), p) is False


def test_args_without_non_interactive_attr_is_interactive(tmp_path: Path, monkeypatch) -> None:
    """Args object that just doesn't carry the attribute should be
    treated as interactive (the default); avoids tripping over
    argparse defaults that the caller forgot to wire in."""
    wiz = _make_wizard_script(tmp_path)
    # The script-missing path returns False before we even check the
    # arg; this test really just asserts no AttributeError.
    ns = argparse.Namespace()    # no .non_interactive
    is_wizard_available(ns, wiz)   # must not raise
    is_wizard_available(ns, tmp_path / "nope.sh")  # must not raise


# ---------- exec_wizard ----------------------------------------------------

def test_exec_wizard_kv_parse(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo STATUS_ADDRESS=bee1-status.local\n'
        'echo INSTANCE=bee1-rx888\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="kv")
    assert result.ok
    assert result.fields == {
        "status_address": "bee1-status.local",
        "instance":       "bee1-rx888",
    }


def test_exec_wizard_kv_skips_lines_without_equals(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo "this is informational"\n'
        'echo "KEY=value"\n'
        'echo ""\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="kv")
    assert result.fields == {"key": "value"}


def test_exec_wizard_kv_empty_stdout_is_clean_cancel(tmp_path: Path) -> None:
    """The wspr-recorder contract: exit 0 + empty stdout = operator
    cancelled cleanly or chose Edit-TOML."""
    wiz = _make_wizard_script(tmp_path, '#!/bin/bash\nexit 0\n')
    result = exec_wizard(wiz, parse="kv")
    assert result.ok
    assert result.fields == {}


def test_exec_wizard_json_parse(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        "echo '{\"station\": {\"callsign\": \"AC0G\"}}'\n"
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="json")
    assert result.ok
    assert result.fields == {"station": {"callsign": "AC0G"}}


def test_exec_wizard_json_rejects_array_top_level(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        "echo '[1,2,3]'\n"
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="json")
    assert not result.ok
    assert result.error is not None
    assert "expected object" in result.error


def test_exec_wizard_json_rejects_invalid(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo "not json"\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="json")
    assert not result.ok
    assert "not valid JSON" in result.error


def test_exec_wizard_parse_none_returns_no_fields(tmp_path: Path) -> None:
    """parse=None means the wizard writes its own side effects (e.g.
    psk-recorder's wizard which pipes JSON to `config apply` itself).
    .fields is None; the caller uses .returncode."""
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo "wrote /etc/foo.toml"\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse=None)
    assert result.ok
    assert result.fields is None


def test_exec_wizard_parse_none_defaults_to_interactive_stdio(tmp_path: Path) -> None:
    """REGRESSION: when parse=None, the wizard's stdio must NOT be
    captured -- the operator needs to see whiptail's UI rendering on
    their terminal.  Earlier version unconditionally used
    capture_output=True, which made mag-recorder's `config edit` exit
    silently with no UI visible (the operator saw nothing).

    Verify by spawning a child that prints a unique sentinel to its
    stdout and a different one to stderr, then asserting that both
    appear in this process's captured stdout/stderr (because they
    were inherited).  Run via subprocess so pytest's own capture
    fixture catches them deterministically."""
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo "WIZARD_STDOUT_SENTINEL"\n'
        'echo "WIZARD_STDERR_SENTINEL" >&2\n'
        'exit 0\n',
    )
    # Run the assertion in a child Python so pytest's capsys doesn't
    # interfere with the test of subprocess stdio inheritance.
    sub = subprocess.run(
        ["python3", "-c", f"""
import sys
sys.path.insert(0, '{Path(__file__).resolve().parent.parent / "lib"}')
from sigmond.wizard_dispatch import exec_wizard
r = exec_wizard('{wiz}', parse=None)
print(f'fields={{r.fields!r}} stderr={{r.stderr!r}} returncode={{r.returncode}}')
"""],
        capture_output=True, text=True, check=True,
    )
    # The sentinels must have appeared on the OUTER process's stdio
    # (because the wizard inherited it), not in the WizardResult.
    assert "WIZARD_STDOUT_SENTINEL" in sub.stdout
    assert "WIZARD_STDERR_SENTINEL" in sub.stderr
    # The WizardResult itself reports no captured stderr / fields,
    # since the operator already saw them on their terminal.
    assert "stderr=''" in sub.stdout
    assert "fields=None" in sub.stdout


def test_exec_wizard_interactive_explicit_overrides_default(tmp_path: Path) -> None:
    """interactive=False with parse=None: caller wants neither
    parsing NOR stdio inheritance (rare; for wizards that write
    their UI to /dev/tty directly).  Just verify no exception."""
    wiz = _make_wizard_script(tmp_path, '#!/bin/bash\necho out\nexit 0\n')
    result = exec_wizard(wiz, parse=None, interactive=False)
    assert result.ok
    assert result.fields is None
    # stderr is captured (because interactive=False), but the wizard
    # only printed stdout, so .stderr should be empty.
    assert result.stderr == ""


def test_exec_wizard_kv_explicit_interactive_true_skips_parse(tmp_path: Path) -> None:
    """interactive=True short-circuits parse: stdout went to the
    operator's terminal, so .fields is None regardless of parse=.
    Documented behaviour for the niche case where a caller explicitly
    asks for both interactive stdio and a parse mode."""
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\necho "KEY=value"\nexit 0\n',
    )
    # Run via subprocess so the wizard's stdout is observable on
    # outer stdout (because it was inherited, not captured).
    sub = subprocess.run(
        ["python3", "-c", f"""
import sys
sys.path.insert(0, '{Path(__file__).resolve().parent.parent / "lib"}')
from sigmond.wizard_dispatch import exec_wizard
r = exec_wizard('{wiz}', parse='kv', interactive=True)
print(f'fields={{r.fields!r}}')
"""],
        capture_output=True, text=True, check=True,
    )
    assert "KEY=value" in sub.stdout         # wizard's echo inherited
    assert "fields=None" in sub.stdout       # parse skipped because interactive


def test_exec_wizard_returncode_passed_through(tmp_path: Path) -> None:
    wiz = _make_wizard_script(tmp_path, '#!/bin/bash\nexit 7\n')
    result = exec_wizard(wiz, parse="kv")
    assert result.returncode == 7
    assert result.fields == {}      # still parsed (was empty)
    assert result.error is None     # not an error, just non-zero rc


def test_exec_wizard_stderr_captured(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo "diagnostic" >&2\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="kv")
    assert "diagnostic" in result.stderr


def test_exec_wizard_missing_script_returns_error(tmp_path: Path) -> None:
    """OSError on exec is surfaced via .error, not raised.  Caller
    falls back to its legacy stdin path."""
    result = exec_wizard(tmp_path / "absent.sh", parse="kv")
    assert not result.ok
    assert result.error is not None
    assert "exec failed" in result.error


def test_exec_wizard_extra_env_visible_to_child(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'echo "GOT=${MY_VAR:-unset}"\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="kv", extra_env={"MY_VAR": "hello"})
    assert result.fields == {"got": "hello"}


def test_exec_wizard_extra_args_forwarded(tmp_path: Path) -> None:
    wiz = _make_wizard_script(
        tmp_path,
        '#!/bin/bash\n'
        'printf "MODE=%s\\n" "$1"\n'
        'exit 0\n',
    )
    result = exec_wizard(wiz, parse="kv", extra_args=["edit"])
    assert result.fields == {"mode": "edit"}


def test_exec_wizard_rejects_unknown_parse_mode(tmp_path: Path) -> None:
    wiz = _make_wizard_script(tmp_path)
    with pytest.raises(ValueError, match="parse"):
        exec_wizard(wiz, parse="toml")    # not a supported mode


# ---------- wizard_dispatch.sh: source-and-check ---------------------------

_LIB_SH = Path(__file__).resolve().parent.parent \
    / "lib" / "sigmond" / "wizard_dispatch" / "wizard_dispatch.sh"


def test_shell_helpers_source_cleanly() -> None:
    """The shell library must be source-able and the helpers must
    expand to the documented defaults."""
    result = subprocess.run(
        ["bash", "-c", f". {_LIB_SH}; echo W=$WIDTH; echo H=$HEIGHT; echo L=$LIST_HEIGHT"],
        capture_output=True, text=True, check=True,
    )
    out = result.stdout
    assert "W=78" in out
    assert "H=20" in out
    assert "L=10" in out


def test_shell_helpers_loggers_write_to_stderr() -> None:
    result = subprocess.run(
        ["bash", "-c", f". {_LIB_SH}; _info infomsg; _warn warnmsg; _err errmsg"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == ""
    assert "infomsg" in result.stderr
    assert "warnmsg" in result.stderr
    assert "errmsg"  in result.stderr


def test_shell_preflight_or_exit_2_no_whiptail() -> None:
    """preflight_or_exit_2 in a no-whiptail PATH must exit 2, not 1."""
    result = subprocess.run(
        ["bash", "-c",
         f"export PATH=/nonexistent; . {_LIB_SH}; preflight_or_exit_2"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2


def test_shell_preflight_or_exit_2_no_tty() -> None:
    """No TTY also exits 2.  In subprocess.run stdout is a pipe, so
    `[[ -t 1 ]]` is false even if whiptail were installed."""
    if subprocess.run(["which", "whiptail"], capture_output=True).returncode != 0:
        pytest.skip("whiptail not installed; can't isolate the no-TTY check")
    result = subprocess.run(
        ["bash", "-c", f". {_LIB_SH}; preflight_or_exit_2"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
