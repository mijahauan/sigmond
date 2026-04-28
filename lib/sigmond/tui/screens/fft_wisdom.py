"""FFT Wisdom planning screen.

Lets the operator generate or refresh the FFTW wisdom file that radiod
needs before it can start cleanly.  Wisdom generation can take hours for
the largest transforms (rof3240000 on an RX888 @ 129.6 MHz); smaller
channel-inverse transforms complete in seconds.

This screen:
  - Shows wisdom file status (present/missing, size, age)
  - Stops all sigmond-managed services before planning
  - Spawns fftwf-wisdom with stdbuf for line-buffered live output
  - Pins the planner process to the CPU it lands on (prevents migration)
  - Streams progress to a live RichLog widget
  - Installs wisdomf.new → wisdomf on success
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import time
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Static


# ---------------------------------------------------------------------------
# Constants (mirror bin/smd — these are stable public paths)
# ---------------------------------------------------------------------------

_FFTWF_WISDOM_BIN = Path('/usr/bin/fftwf-wisdom')
_WISDOM_FILE      = Path('/etc/fftw/wisdomf')
_WISDOM_TMP       = Path('/etc/fftw/wisdomf.new')
_WISDOM_LOG       = Path('/tmp/ka9q-wisdom.log')
_WISDOM_PID       = Path('/var/run/ka9q-fft-wisdom.pid')

_FFT_WISDOM_PROFILES = [
    # Inverse FFTs for demodulator channels — smallest first
    'cob15',   'cob45',   'cob85',
    'cob160',  'cob200',  'cob205',  'cob300',   'cob320',
    'cob400',  'cob405',  'cob480',  'cob600',   'cob800',  'cob810',
    'cob960',  'cob1200', 'cob1600', 'cob1620',  'cob1920',
    'cob3200', 'cob3240', 'cob4800', 'cob4860',  'cob6930',
    'cob8100', 'cob9600', 'cob16200', 'cob32400', 'cob40500',
    'cob81000', 'cob162000',
    # Forward real FFTs — progressively larger, most expensive last
    'rof1620000',   # RX888 MkII @  64.8 MHz, 20 ms block, overlap 5
    'rof3240000',   # RX888 MkII @ 129.6 MHz, 20 ms block, overlap 5  ← hours
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _smd_binary() -> str:
    import os, sys
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


def _wisdom_status() -> dict:
    """Return a dict describing the current state of the wisdom file."""
    in_progress = False
    if _WISDOM_PID.exists():
        try:
            pid = int(_WISDOM_PID.read_text().strip())
            Path(f'/proc/{pid}').stat()
            in_progress = True
        except (ValueError, OSError):
            _WISDOM_PID.unlink(missing_ok=True)

    if not _WISDOM_FILE.exists():
        return {'present': False, 'in_progress': in_progress}

    stat = _WISDOM_FILE.stat()
    age_s = time.time() - stat.st_mtime
    if age_s < 86400:
        age_str = f"{int(age_s / 3600)}h {int((age_s % 3600) / 60)}m ago"
    else:
        age_str = f"{int(age_s / 86400)}d ago"
    return {
        'present': True,
        'in_progress': in_progress,
        'size': stat.st_size,
        'age': age_str,
    }


def _pin_to_current_cpu(pid: int) -> None:
    """Pin process to whichever CPU it landed on to prevent migration."""
    taskset = shutil.which('taskset')
    if not taskset:
        return
    try:
        stat_text = Path(f'/proc/{pid}/stat').read_text()
        # comm field may contain spaces; find closing ')'
        after_comm = stat_text[stat_text.rfind(')') + 2:]
        fields = after_comm.split()
        cpu = int(fields[36])   # field 39 in /proc/pid/stat (0-indexed from state)
        subprocess.run([taskset, '-cp', str(cpu), str(pid)],
                       capture_output=True, timeout=2)
    except Exception:
        pass


def _size_of(profile: str) -> int:
    """Extract the numeric transform size from a profile name like 'rof3240000'."""
    m = re.search(r'\d+', profile)
    return int(m.group()) if m else 1


def _fmt_dur(seconds: float) -> str:
    """Format a duration as '2.3s', '4m12s', or '1h23m45s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds % 60
    if m < 60:
        return f"{m}m{int(s)}s"
    return f"{m // 60}h{m % 60}m{int(s)}s"


# Pre-compute profile matching: sorted longest-first so 'cob162000' matches
# before 'cob1620' before 'cob160' (avoids substring false-positives).
_PROFILES_BY_LEN = sorted(_FFT_WISDOM_PROFILES, key=len, reverse=True)
_PROFILE_SET     = set(_FFT_WISDOM_PROFILES)
_PROFILE_INDEX   = {p: i for i, p in enumerate(_FFT_WISDOM_PROFILES)}


def _match_profile(line: str) -> str | None:
    """Return the first profile name that appears as a whole word in line."""
    for profile in _PROFILES_BY_LEN:
        if re.search(rf'\b{re.escape(profile)}\b', line):
            return profile
    return None


def _install_wisdom_tui() -> str:
    """Install wisdomf.new → wisdomf.  Returns status message."""
    if not _WISDOM_TMP.exists():
        return 'wisdomf.new not found'
    new_size = _WISDOM_TMP.stat().st_size
    if new_size == 0:
        _WISDOM_TMP.unlink(missing_ok=True)
        return 'wisdomf.new was empty — discarded'
    old_size = _WISDOM_FILE.stat().st_size if _WISDOM_FILE.exists() else 0
    if new_size >= old_size:
        r = subprocess.run(
            ['sudo', 'mv', str(_WISDOM_TMP), str(_WISDOM_FILE)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return f'wisdom installed ({new_size:,} bytes)'
        return f'install failed: {r.stderr.strip()}'
    _WISDOM_TMP.unlink(missing_ok=True)
    return (f'new wisdom ({new_size:,} B) smaller than existing '
            f'({old_size:,} B) — kept old file')


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

class FFTWisdomScreen(Vertical):
    """FFT wisdom planning — generate/refresh the wisdomf file for radiod."""

    DEFAULT_CSS = """
    FFTWisdomScreen {
        padding: 1;
    }
    FFTWisdomScreen #wis-title {
        text-style: bold;
        margin-bottom: 0;
    }
    FFTWisdomScreen #wis-status {
        margin-bottom: 1;
        color: $text-muted;
    }
    FFTWisdomScreen #wis-warn {
        margin-bottom: 1;
    }
    FFTWisdomScreen #wis-btn-row {
        height: 3;
        margin-bottom: 1;
    }
    FFTWisdomScreen #wis-btn-row Button {
        margin-right: 1;
    }
    FFTWisdomScreen #wis-log {
        height: 1fr;
        border: solid $primary-background;
    }
    """

    def compose(self):
        yield Static("FFT Wisdom Planning", id="wis-title")
        yield Static("", id="wis-status")
        yield Static("", id="wis-warn")
        with Horizontal(id="wis-btn-row"):
            yield Button("▶ Run planning", id="wis-run", variant="warning")
            yield Button("↺ Refresh status", id="wis-refresh", variant="default")
        yield RichLog(id="wis-log", highlight=False, markup=False,
                      max_lines=5000, wrap=True)

    def on_mount(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        s = _wisdom_status()
        status_w = self.query_one("#wis-status", Static)
        warn_w   = self.query_one("#wis-warn", Static)

        if s['in_progress']:
            status_w.update(
                f"[yellow]⏳ planning in progress "
                f"— tail -f {_WISDOM_LOG} to watch[/]"
            )
            warn_w.update(
                "[yellow]⚠ radiod cannot start until planning completes. "
                "Run Apply once this screen reports success.[/]"
            )
            self.query_one("#wis-run", Button).disabled = True
        elif not s['present']:
            status_w.update("[red]✗ wisdom file missing — radiod will fail[/]")
            warn_w.update(
                "[yellow]⚠ Click 'Run planning' to generate the wisdom file. "
                "Small transforms complete in seconds; "
                "rof3240000 (RX888 @ 129.6 MHz) can take hours on first run.[/]"
            )
            self.query_one("#wis-run", Button).disabled = False
        else:
            size = s.get('size', 0)
            age  = s.get('age', '')
            status_w.update(
                f"[green]✔ wisdom present[/]  "
                f"[dim]{size:,} bytes  ·  {age}[/]"
            )
            warn_w.update("")
            self.query_one("#wis-run", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wis-run":
            self._start_planning()
        elif event.button.id == "wis-refresh":
            self._refresh_status()

    def _start_planning(self) -> None:
        if not _FFTWF_WISDOM_BIN.exists():
            self.query_one("#wis-warn", Static).update(
                f"[red]fftwf-wisdom not found at {_FFTWF_WISDOM_BIN}[/]"
            )
            return
        btn = self.query_one("#wis-run", Button)
        btn.disabled = True
        self.query_one("#wis-log", RichLog).clear()
        self.run_worker(self._planning_worker, thread=True, name="wis-planning")

    # ------------------------------------------------------------------
    def _planning_worker(self) -> None:
        """Worker thread: stop services → run fftwf-wisdom → install."""
        log = self.query_one("#wis-log", RichLog)

        def emit(line: str) -> None:
            self.app.call_from_thread(log.write, line)

        smd_bin = _smd_binary()

        # Step 1 — stop all managed services
        emit("─── Stopping all managed services ──────────────────────────")
        r = subprocess.run(
            ['sudo', smd_bin, 'stop'],
            capture_output=True, text=True,
        )
        for line in (r.stdout + r.stderr).splitlines():
            emit(line)
        emit("")

        # Step 2 — spawn fftwf-wisdom with line-buffered output
        emit(f"─── Starting fftwf-wisdom ({len(_FFT_WISDOM_PROFILES)} profiles) ──────────")
        emit(f"    Profiles: {_FFT_WISDOM_PROFILES[0]} … {_FFT_WISDOM_PROFILES[-1]}")
        emit(f"    Log:      {_WISDOM_LOG}")
        emit("")

        _stdbuf = shutil.which('stdbuf')
        cmd = (
            [_stdbuf, '-oL'] if _stdbuf else []
        ) + [str(_FFTWF_WISDOM_BIN), '-v', '-T', '1',
             '-o', str(_WISDOM_TMP)] + _FFT_WISDOM_PROFILES

        try:
            with open(_WISDOM_LOG, 'w') as log_f:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            emit(f"[error starting fftwf-wisdom: {exc}]")
            self.app.call_from_thread(self._refresh_status)
            self.app.call_from_thread(
                lambda: setattr(self.query_one("#wis-run", Button), 'disabled', False)
            )
            return

        # Write PID file for smd apply to detect
        try:
            _WISDOM_PID.write_text(str(proc.pid))
        except OSError:
            pass

        # Pin to the CPU it landed on (prevents migration during long run)
        time.sleep(0.05)   # give OS a moment to schedule it
        _pin_to_current_cpu(proc.pid)

        # Step 3 — stream log output with timestamps and profile predictions
        #
        # Each line is prefixed:  HH:MM:SS  +MM:SS.s  <content>
        #   HH:MM:SS  = wall-clock time the line appeared
        #   +MM:SS.s  = seconds since the previous output line
        #
        # When a profile name is first seen in the output, a prediction line
        # is emitted for the next profile based on the size-ratio rule:
        #   planning time ∝ (n_next / n_prev)²   (user-observed heuristic)
        last_line_mono  = time.monotonic()
        seen_profiles: set = set()
        profile_events: list = []   # [(profile_name, monotonic_time), ...]

        def _emit_timestamped(raw: str) -> None:
            nonlocal last_line_mono
            now  = time.monotonic()
            wall = datetime.datetime.now().strftime('%H:%M:%S')
            delta = now - last_line_mono
            dm, ds = int(delta // 60), delta % 60
            last_line_mono = now
            emit(f"{wall}  +{dm:02d}:{ds:04.1f}  {raw.rstrip()}")

        try:
            with open(_WISDOM_LOG, 'r') as lf:
                while True:
                    raw = lf.readline()
                    if raw:
                        _emit_timestamped(raw)

                        # Profile first-appearance detection
                        profile = _match_profile(raw)
                        if profile and profile not in seen_profiles:
                            seen_profiles.add(profile)
                            profile_events.append((profile, time.monotonic()))

                            if len(profile_events) >= 2:
                                prev_name, prev_t = profile_events[-2]
                                cur_name,  cur_t  = profile_events[-1]
                                duration = cur_t - prev_t

                                # Predict the profile after the current one
                                cur_idx = _PROFILE_INDEX.get(cur_name, -1)
                                if cur_idx >= 0 and cur_idx + 1 < len(_FFT_WISDOM_PROFILES):
                                    nxt = _FFT_WISDOM_PROFILES[cur_idx + 1]
                                    ratio = _size_of(nxt) / max(_size_of(cur_name), 1)
                                    pred  = duration * ratio ** 2
                                    emit(
                                        f"{'':>22}  ↳ {prev_name} took "
                                        f"{_fmt_dur(duration)}  |  "
                                        f"next: {nxt}  ~{_fmt_dur(pred)}"
                                    )

                    elif proc.poll() is not None:
                        for raw in lf:
                            _emit_timestamped(raw)
                        break
                    else:
                        time.sleep(0.15)
        except Exception as exc:
            emit(f"[log read error: {exc}]")

        # Step 4 — install / report
        emit("")
        emit("─── Done ───────────────────────────────────────────────────")
        _WISDOM_PID.unlink(missing_ok=True)
        if proc.returncode == 0:
            msg = _install_wisdom_tui()
            emit(f"✔ {msg}")
            emit("radiod can now be started — run Apply from the Operate menu.")
        else:
            emit(f"✗ fftwf-wisdom exited {proc.returncode} — check {_WISDOM_LOG}")

        self.app.call_from_thread(self._refresh_status)
        self.app.call_from_thread(
            lambda: setattr(self.query_one("#wis-run", Button), 'disabled', False)
        )
