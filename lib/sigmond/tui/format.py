"""Pure-Python formatting helpers for TUI screens.

This module has NO Textual imports — every function here is a plain
string formatter or value mapper.  That keeps the helpers
unit-testable in environments where Textual is not installed
(e.g. CI without GUI deps), which most of the screen modules cannot
support because Textual is imported at module top level.

Add helpers here when they:

- Are pure functions of their inputs.
- Are referenced by ``screens/*.py`` for rendering.
- Don't need any Textual widget or container types.
"""

from __future__ import annotations

from typing import Optional


def format_timing_line(inst) -> Optional[str]:
    """Render a CLIENT-CONTRACT v0.7 §18 timing-state line for one
    ``InstanceView``, or ``None`` if the instance is in the boring
    default case (no §18 role, nothing worth surfacing).

    The Overview screen calls this once per instance to produce a
    sub-line under each client entry.  Returning ``None`` lets the
    common-case rendering stay compact: only instances with an
    interesting §18 role contribute a "timing: …" line.

    Cases (mutually exclusive, in priority order):

    1. ``provides_timing_calibration=True`` — the instance is itself
       a §18 timing-authority producer.  Visually distinctive (green)
       because there's typically one per station and operators want
       to confirm it's there.
    2. ``timing_authority_applied`` is a populated dict — the instance
       is actively subscribing.  Show ``tier / σ / age (source)`` so
       an operator can read the budget at a glance.  Colour by tier
       quality: green for T5+, yellow for T4, red for ≤T3.
    3. ``uses_timing_calibration=True`` but ``timing_authority_applied``
       is None — the client is capable of subscribing but is currently
       in default mode (either no authority is reachable or it's been
       gated off).  Yellow, slightly verbose so the operator knows
       why nothing is happening.
    4. All other cases — return ``None`` (no line emitted).
    """
    if getattr(inst, 'provides_timing_calibration', False):
        return "[green]provides authority[/]"

    applied = getattr(inst, 'timing_authority_applied', None)
    if isinstance(applied, dict):
        tier   = applied.get('tier') or '?'
        source = applied.get('source') or '?'
        sigma  = applied.get('sigma_ns')
        age    = applied.get('snapshot_age_s')

        # σ in ns; auto-scale to the most natural unit, matching the
        # convention in timing.py's format_offset.
        if isinstance(sigma, (int, float)):
            if sigma < 1_000:
                sigma_str = f"σ={sigma:g} ns"
            elif sigma < 1_000_000:
                sigma_str = f"σ={sigma / 1_000:.2g} µs"
            else:
                sigma_str = f"σ={sigma / 1_000_000:.2g} ms"
        else:
            sigma_str = "σ=?"

        if isinstance(age, (int, float)):
            age_str = f"age={age:.1f}s" if age < 60 else f"age={age / 60:.1f}m"
        else:
            age_str = "age=?"

        # Tier-quality colour per ARCHITECTURE-FIRST-PRINCIPLES.md §2
        # (post-2026-05-24 rerank): T5 / T6 are ns-class hard-wired
        # paths (green); T4 is µs-to-ms LAN/USB (yellow); T0–T3 are
        # ms-class or worse (red), inadequate for hard-deadline
        # gating but still useful for sample-labelling clients.
        if tier in ('T5', 'T6'):
            colour = 'green'
        elif tier == 'T4':
            colour = 'yellow'
        else:
            colour = 'red'
        return f"[{colour}]{tier}[/] {sigma_str} {age_str}  source={source}"

    if getattr(inst, 'uses_timing_calibration', False):
        return ("[yellow]subscriber-capable, currently default mode[/] "
                "(no §18 authority applied)")

    return None
