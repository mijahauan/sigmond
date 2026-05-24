# sigmond.wizard_dispatch — Tier-1 helpers for per-client whiptail wizards

**Status: DRAFT (not yet consumed by any client).**  Captures the dispatch
contract already implemented identically in three independent
per-client wizards.  Lives here for review before any client is
refactored to consume it.

## Why this exists

Three sigmond clients (`mag-recorder`, `psk-recorder`, `wspr-recorder`)
each ship a `scripts/config-wizard.sh` driven by `whiptail`, dispatched
from their `configurator.py`'s `cmd_config_init` / `cmd_config_edit`.
They follow the same dispatch contract — same TTY/whiptail/script-exists
gate, same fallback semantics, same `--non-interactive` opt-out, same
env-var pre-fill protocol — but each re-implements that contract from
scratch.  ~50 lines per client of pure scaffolding, no client-specific
content.

This subpackage extracts those ~50 lines so the fourth client can
import them instead of copy-pasting.

## What this is and isn't

**This IS** (Tier 1, ~50 lines):

* `wizard_dispatch.py` — Python-side helpers:
  * `is_wizard_available(args, wizard_path) -> bool` — gate check
    used by `cmd_config_init` / `cmd_config_edit`
  * `exec_wizard(wizard_path, *, extra_env, parse) -> Optional[dict|None]`
    — spawn the shell wizard, forward args, parse stdout, swallow
    OSError into None.  Two parse modes: `parse="kv"` for the
    wspr-recorder shape (`KEY=VALUE` lines on stdout) and `parse="json"`
    for the mag/psk-recorder shape (single JSON document).
* `wizard_dispatch.sh` — shell-side helpers a client wizard can
  `source` for the universal preflight + fallback:
  * `preflight_or_exit_2` — `whiptail` on PATH + stdout is a TTY,
    error out with code 2 if not (matches the contract that
    is_wizard_available() already gated for; this is belt-and-braces
    in case the wizard was invoked directly).
  * `_info` / `_warn` / `_err` — colored stderr loggers (identical
    in all three clients today).
  * Recommended `BACKTITLE`, `HEIGHT`, `WIDTH`, `LIST_HEIGHT` defaults.

**This IS NOT** (Tier 2, deferred):

* the multi-section menu loop (`main_menu_loop`)
* the per-field `ask` helper with `required = false` skip-on-Cancel
  and leading-dash whiptail workaround
* `config show --json` / `config apply --json -` Python entry points
* `_serialize_toml` (the bulk-write TOML emitter)
* the per-key `help.toml` schema

Those are reusable across mag-recorder and psk-recorder but explicitly
NOT used by wspr-recorder's narrower pattern.  Extracting them would
force wspr-recorder to take on machinery it doesn't need.  If a fourth
client adopts the full-walker pattern, Tier 2 becomes the next library
to extract.

## The observed contract (from the three existing call sites)

Every client's `cmd_config_init` / `cmd_config_edit` does this:

```python
def cmd_config_init(args):
    if non_interactive(args):
        return _legacy_init(args)
    if is_wizard_available(args, _WIZARD_PATH):
        return _exec_wizard_and_apply(args, "init")
    return _legacy_init(args)
```

Where `is_wizard_available` is identical across all three (modulo
package name):

* `args.non_interactive` is False
* `sys.stdout.isatty()` and `sys.stdin.isatty()` are both True
* `shutil.which("whiptail")` is not None
* `_WIZARD_PATH.is_file()` and `os.access(_WIZARD_PATH, os.X_OK)`

Every wizard sets two env vars before exec:

* `<CLIENT>_HELP_TOML` — path to the help sidecar (mag/psk only;
  wspr-recorder has no per-key help)
* `<CLIENT>_CLI` — `sys.argv[0]`, so the shell wizard can shell out
  back to the same Python entry point the operator invoked

And every wizard handles fallback identically: if the operator
cancels or `whiptail` fails, the legacy stdin-prompt path runs.

## Adoption sketch (per client)

For wspr-recorder (Rob's pattern):

```python
from sigmond.wizard_dispatch import is_wizard_available, exec_wizard

def cmd_config_edit(args):
    target = _resolve_target(args)
    if is_wizard_available(args, _WIZARD_PATH):
        fields = exec_wizard(
            _WIZARD_PATH,
            extra_env={"WSPR_RECORDER_CONFIG": str(target)},
            parse="kv",
        )
        if fields is None:        # real error -> legacy fallback
            return _legacy_edit(args)
        if fields:                # operator chose something -> apply
            return _apply_fields(target, fields)
        return 0                  # cancel / edit-toml: no apply needed
    return _legacy_edit(args)
```

For mag-recorder / psk-recorder (my pattern):

```python
from sigmond.wizard_dispatch import is_wizard_available, exec_wizard

def cmd_config_edit(args):
    if is_wizard_available(args, _WIZARD_PATH):
        return exec_wizard(
            _WIZARD_PATH,
            extra_env={
                "MAG_RECORDER_CLI":       sys.argv[0],
                "MAG_RECORDER_HELP_TOML": str(_HELP_TOML_PATH),
            },
            parse=None,           # wizard writes via `config apply` directly
        ).returncode
    return _legacy_edit(args)
```

(For the JSON-apply-on-its-own pattern, the wizard never echoes data
on stdout; `parse=None` returns the raw `subprocess.CompletedProcess`
and the caller uses `.returncode`.)

## Open questions before adoption

1. **Package surface.** `sigmond.wizard_dispatch` vs.
   `sigmond.lib.wizard_dispatch` vs. a separate top-level package.
   Today sigmond is core-stdlib-only; clients lazy-import sigmond
   submodules already (`sigmond.hamsci_sink.Writer`), so adding
   `sigmond.wizard_dispatch` follows that precedent.
2. **Shell-side packaging.** `wizard_dispatch.sh` lives in this
   subpackage's directory, but a client's `scripts/config-wizard.sh`
   needs to `source` it.  Options:
   * Hard-code `source /opt/git/sigmond/sigmond/lib/sigmond/wizard_dispatch/wizard_dispatch.sh`
     (matches sigmond's install convention; brittle to relocation).
   * Have the Python side `exec_wizard` set `SIGMOND_WIZARD_LIB_SH`
     and the client wizard sources from there (cleaner).
3. **Version policy.** Once a client consumes this, the contract is
   binding.  Bumping the dispatch API would force coordinated
   updates.  Worth versioning the module from day one
   (`SIGMOND_WIZARD_DISPATCH_API = "1"`)?

These are the things to decide before flipping the first client over
to consume this.
