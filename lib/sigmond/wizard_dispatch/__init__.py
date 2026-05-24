"""sigmond.wizard_dispatch — Tier-1 helpers for per-client whiptail wizards.

DRAFT.  See README.md in this directory for status and the observed
contract this module captures.

Re-exports the two public helpers from the implementation module so
the public API is `sigmond.wizard_dispatch.{is_wizard_available,
exec_wizard}`.
"""

from .wizard_dispatch import (   # noqa: F401
    SIGMOND_WIZARD_DISPATCH_API,
    is_wizard_available,
    exec_wizard,
    WizardResult,
)

__all__ = [
    "SIGMOND_WIZARD_DISPATCH_API",
    "is_wizard_available",
    "exec_wizard",
    "WizardResult",
]
