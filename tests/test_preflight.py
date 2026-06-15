"""Tests for the pre-flight requirements check."""

from __future__ import annotations

import io
import time
from unittest import mock

import pytest

from sigmond import preflight


# ---------------------------------------------------------------------------
# Fake catalog entry — CatalogEntry is frozen, so instance-level
# monkeypatching of is_installed() isn't possible.  This stand-in
# duck-types the attributes preflight + catalog helpers actually read.
# ---------------------------------------------------------------------------

class _FakeEntry:
    def __init__(self, name, *, requires=(), kind="client",
                 install_script="", installed=False):
        self.name = name
        self.kind = kind
        self.requires = tuple(requires)
        self.install_script = install_script
        self.topology_alias = ""
        self._installed = installed

    def is_installed(self) -> bool:
        return self._installed


def _entry(name, **kwargs):
    return _FakeEntry(name, **kwargs)


def _cache(observations=None, probed_at=None):
    return {
        "probed_at":     probed_at if probed_at is not None else time.time(),
        "observations":  observations or [],
        "deltas":        [],
    }


def _radiod_obs(name="bee1", endpoint="bee1-status.local:5006"):
    return {
        "source": "mdns", "kind": "radiod", "id": None,
        "endpoint": endpoint, "fields": {"name": name},
        "observed_at": time.time(), "ok": True, "error": "",
    }


def _sdr_obs(sdr_type="RX-888 DFU", bus="003", device="008"):
    return {
        "source": "usb_sdr", "kind": "sdr", "id": "usb:04b4:00f3:0",
        "endpoint": f"bus {bus} dev {device}",
        "fields": {"sdr_type": sdr_type, "bus": bus, "device": device,
                   "vid": "04b4", "pid": "00f3", "index": 0, "serial": ""},
        "observed_at": time.time(), "ok": True, "error": "",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def catalog_with_unmet_ka9q():
    """wspr-recorder requires ka9q-radio; ka9q-radio is NOT installed."""
    return {
        "wspr-recorder": _entry("wspr-recorder",
                                 requires=["ka9q-python", "ka9q-radio"],
                                 installed=False),
        "ka9q-python":   _entry("ka9q-python", kind="library", installed=True),
        "ka9q-radio":    _entry("ka9q-radio", kind="server", installed=False),
    }


@pytest.fixture
def catalog_all_met():
    return {
        "wspr-recorder": _entry("wspr-recorder",
                                 requires=["ka9q-python", "ka9q-radio"]),
        "ka9q-python":   _entry("ka9q-python", kind="library", installed=True),
        "ka9q-radio":    _entry("ka9q-radio", kind="server", installed=True),
    }


@pytest.fixture
def cache_radiods_only():
    """Remote radiods on the LAN, no local SDR."""
    return _cache(observations=[_radiod_obs("bee1"),
                                 _radiod_obs("bee2", "bee2-status.local:5006")])


@pytest.fixture
def cache_radiods_and_sdr():
    """Remote radiods AND a local SDR."""
    return _cache(observations=[_radiod_obs("bee1"), _sdr_obs()])


@pytest.fixture
def cache_sdr_only():
    """Local SDR but no remote radiod."""
    return _cache(observations=[_sdr_obs()])


@pytest.fixture
def cache_empty():
    """Cache populated (probed_at > 0) but no relevant observations."""
    return _cache(observations=[])


@pytest.fixture
def no_cache():
    return {"probed_at": 0.0, "observations": [], "deltas": []}


def _make_tty_stdin(monkeypatch, response="\n"):
    fake = io.StringIO(response)
    fake.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", fake)
    monkeypatch.setattr("builtins.input", lambda prompt="": response.rstrip("\n"))


# ---------------------------------------------------------------------------
# Happy path — nothing missing
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_nothing_missing_proceeds(self, catalog_all_met):
        with mock.patch.object(preflight, "load_cache") as m_load:
            assert preflight.check_requires("wspr-recorder",
                                             catalog_all_met,
                                             yes=False) is True
            assert not m_load.called

    def test_unknown_client_proceeds(self, catalog_all_met):
        assert preflight.check_requires("nonesuch",
                                         catalog_all_met,
                                         yes=True) is True


# ---------------------------------------------------------------------------
# No cache yet → must direct operator to probe
# ---------------------------------------------------------------------------

class TestNoCache:
    def test_aborts_without_yes(self, catalog_with_unmet_ka9q, no_cache):
        with mock.patch.object(preflight, "load_cache", return_value=no_cache):
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=False) is False

    def test_proceeds_with_yes(self, catalog_with_unmet_ka9q, no_cache):
        with mock.patch.object(preflight, "load_cache", return_value=no_cache):
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=True) is True

    def test_message_mentions_probe(self, catalog_with_unmet_ka9q,
                                     no_cache, capsys):
        with mock.patch.object(preflight, "load_cache", return_value=no_cache):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=True)
        assert "smd admin environment probe" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Remote radiod on LAN → satisfies ka9q-radio dep
# ---------------------------------------------------------------------------

class TestLanSatisfies:
    def test_remote_radiod_satisfies(self, catalog_with_unmet_ka9q,
                                       cache_radiods_only):
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_only):
            # No SDR present — should proceed silently (no prompt).
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=False) is True

    def test_satisfied_message_shows_radiods(self, catalog_with_unmet_ka9q,
                                               cache_radiods_only, capsys):
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_only):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=False)
        captured = capsys.readouterr().err
        assert "satisfied" in captured.lower()
        assert "bee1" in captured

    def test_no_warn_emoji_when_satisfied(self, catalog_with_unmet_ka9q,
                                            cache_radiods_only, capsys):
        # The satisfied path should use ok(), not warn() — no `dependencies
        # that aren't all satisfied:` text.
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_only):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=False)
        captured = capsys.readouterr().err
        assert "aren't all satisfied" not in captured


# ---------------------------------------------------------------------------
# Remote radiod + local SDR → optional install prompt
# ---------------------------------------------------------------------------

class TestLocalSdrPrompt:
    def test_yes_bypasses_prompt(self, catalog_with_unmet_ka9q,
                                   cache_radiods_and_sdr):
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_and_sdr):
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=True) is True

    def test_non_tty_proceeds(self, catalog_with_unmet_ka9q,
                                cache_radiods_and_sdr, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_and_sdr):
            # Non-TTY: no prompt, just proceed with the original install
            # (since the LAN satisfies the dep).
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=False) is True

    def test_tty_user_declines_local_install_proceeds(self,
            catalog_with_unmet_ka9q, cache_radiods_and_sdr, monkeypatch):
        _make_tty_stdin(monkeypatch, "n\n")
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_and_sdr):
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=False) is True

    def test_tty_user_wants_local_install_aborts(self,
            catalog_with_unmet_ka9q, cache_radiods_and_sdr, monkeypatch):
        _make_tty_stdin(monkeypatch, "y\n")
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_and_sdr):
            # User chose to install ka9q-radio first; abort current install.
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=False) is False

    def test_prompt_mentions_sdr_and_followups(self,
            catalog_with_unmet_ka9q, cache_radiods_and_sdr, monkeypatch,
            capsys):
        _make_tty_stdin(monkeypatch, "y\n")
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_radiods_and_sdr):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=False)
        captured = capsys.readouterr().err
        assert "RX-888" in captured
        assert "smd install ka9q-radio" in captured


# ---------------------------------------------------------------------------
# No remote radiod, no local SDR → full warning + prompt
# ---------------------------------------------------------------------------

class TestNoUpstream:
    def test_no_upstream_aborts_without_yes(self, catalog_with_unmet_ka9q,
                                              cache_empty, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_empty):
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=False) is False

    def test_no_upstream_proceeds_with_yes(self, catalog_with_unmet_ka9q,
                                             cache_empty):
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_empty):
            assert preflight.check_requires("wspr-recorder",
                                             catalog_with_unmet_ka9q,
                                             yes=True) is True

    def test_no_upstream_message_says_no_data_source(self,
            catalog_with_unmet_ka9q, cache_empty, capsys):
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_empty):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=True)
        captured = capsys.readouterr().err.lower()
        assert "no data source" in captured or "no local sdr" in captured


# ---------------------------------------------------------------------------
# Local SDR present, no remote radiod → recommend local install + warn
# ---------------------------------------------------------------------------

class TestLocalSdrNoRemote:
    def test_recommends_local_install(self, catalog_with_unmet_ka9q,
                                        cache_sdr_only, capsys):
        with mock.patch.object(preflight, "load_cache",
                                return_value=cache_sdr_only):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=True)
        captured = capsys.readouterr().err
        assert "RX-888" in captured
        assert "smd install ka9q-radio" in captured


# ---------------------------------------------------------------------------
# Stale cache: still consumed, but operator is warned
# ---------------------------------------------------------------------------

class TestStaleCache:
    def test_stale_cache_warns(self, catalog_with_unmet_ka9q, capsys):
        old = time.time() - (2 * 3600)
        cache = _cache(observations=[_radiod_obs("bee1")], probed_at=old)
        with mock.patch.object(preflight, "load_cache", return_value=cache):
            preflight.check_requires("wspr-recorder",
                                      catalog_with_unmet_ka9q,
                                      yes=True)
        captured = capsys.readouterr().err
        assert "min old" in captured


# ---------------------------------------------------------------------------
# Cache only consulted when ka9q-radio is in the missing set
# ---------------------------------------------------------------------------

class TestCacheScope:
    def test_non_ka9q_skips_cache(self):
        catalog = {
            "foo": _entry("foo", requires=["bar"], installed=False),
            "bar": _entry("bar", kind="server", installed=False),
        }
        with mock.patch.object(preflight, "load_cache") as m_load:
            preflight.check_requires("foo", catalog, yes=True)
            assert not m_load.called


# ---------------------------------------------------------------------------
# _unmet_requires
# ---------------------------------------------------------------------------

class TestUnmetRequires:
    def test_empty_when_satisfied(self, catalog_all_met):
        assert preflight._unmet_requires("wspr-recorder", catalog_all_met) == []

    def test_returns_missing(self, catalog_with_unmet_ka9q):
        names = [n for n, _ in preflight._unmet_requires(
            "wspr-recorder", catalog_with_unmet_ka9q)]
        assert names == ["ka9q-radio"]

    def test_unknown_client_empty(self, catalog_all_met):
        assert preflight._unmet_requires("nonesuch", catalog_all_met) == []
