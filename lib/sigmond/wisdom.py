"""FFTW3 wisdom-file planning — paths, profile list, install helper.

Shared between:

  * The TUI screen (``lib/sigmond/tui/screens/fft_wisdom.py``) — runs
    the planner in a worker thread with live progress in a RichLog.
  * The CLI verb (``smd admin wisdom plan``) — runs it in the foreground for
    operators on a tmux/screen session who want to disconnect and
    reconnect over an hours-long planning job.

Both code paths call ``fftwf-wisdom`` with the same profile list so the
generated ``/etc/fftw/wisdomf`` is bit-identical regardless of which
surface the operator chose.

Profile list mirrors ka9q-radio's docs/FFTW3.md recommendations:
inverse FFTs (``cob…``) for every demodulator-channel size radiod
ever uses, plus the forward FFTs (``rof…``) for the two RX-888 sample
rates that dominate planning time on x86 / Pi5.
"""

from __future__ import annotations

from pathlib import Path

# Where the wisdom files live.  Radiod reads system-wide first, then
# the app-specific fallback.  Sigmond plans into the system-wide path
# because it survives package upgrades and is shared across any other
# FFTW user on the host.
WISDOM_FILE = Path('/etc/fftw/wisdomf')
WISDOM_TMP  = Path('/etc/fftw/wisdomf.new')

# Where progress logs land — useful for operators reattaching to a
# screen session and for post-run forensics ("which transform took
# 47 minutes?").
WISDOM_LOG = Path('/tmp/ka9q-wisdom.log')


# Transform sizes to plan, smallest first so quick wins land before
# the multi-hour rof3240000.  Adding a new size: append it here, both
# the TUI progress meter and CLI runner pick it up automatically.
FFT_WISDOM_PROFILES: tuple[str, ...] = (
    # Inverse FFTs for demodulator channels.
    'cob15',   'cob45',   'cob85',
    'cob160',  'cob200',  'cob205',  'cob300',   'cob320',
    'cob400',  'cob405',  'cob480',  'cob600',   'cob800',  'cob810',
    'cob960',  'cob1200', 'cob1600', 'cob1620',  'cob1920',
    'cob3200', 'cob3240', 'cob4800', 'cob4860',  'cob6930',
    'cob8100', 'cob9600', 'cob16200', 'cob32400', 'cob40500',
    'cob81000', 'cob162000',
    # Forward real FFTs.
    'rof1620000',   # RX888 MkII @  64.8 MHz, 20 ms block, overlap 5
    'rof3240000',   # RX888 MkII @ 129.6 MHz, 20 ms block, overlap 5  ← hours
)


def install_wisdom(tmp: Path = WISDOM_TMP, dst: Path = WISDOM_FILE) -> None:
    """Atomically replace ``dst`` with ``tmp`` after a successful plan run.

    Uses rename rather than copy so the swap is atomic — radiod readers
    never see a half-written file.  Both paths are on /etc/fftw so this
    is a same-filesystem rename.
    """
    if not tmp.is_file():
        raise FileNotFoundError(f'{tmp} not present — planning did not finish')
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(dst)
