"""Tests for the individual discovery probes using injected transports.

Each probe module accepts a runner / factory / urlopen override so we
can exercise the parsing + error paths without touching the network."""

import io
import json
import socket
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.environment import (
    DeclaredGpsdo,
    DeclaredKiwi,
    DeclaredRadiod,
    DeclaredTimeSource,
    Environment,
)
from sigmond.discovery import mdns as mdns_mod
from sigmond.discovery import gpsdo as gpsdo_mod
from sigmond.discovery import http_kiwisdr as kiwi_mod
from sigmond.discovery import ntp as ntp_mod
from sigmond.discovery import multicast as mcast_mod
from sigmond.discovery import RateLimiter


def _env(**kw) -> Environment:
    return Environment(
        radiods=kw.get('radiods', []),
        kiwisdrs=kw.get('kiwisdrs', []),
        gpsdos=kw.get('gpsdos', []),
        time_sources=kw.get('time_sources', []),
    )


# ---------------------------------------------------------------------------
# mDNS
# ---------------------------------------------------------------------------

class MdnsProbeTests(unittest.TestCase):
    AVAHI_OUTPUT = """\
=;eth0;IPv4;bee1-hf;_ka9q-ctl._udp;local;bee1.local;192.168.1.10;5006;""
=;eth0;IPv4;kiwi-east;_kiwisdr._tcp;local;kiwi1.local;192.168.1.20;8073;"v1.752"
=;eth0;IPv4;chrony;_ntp._udp;local;time.local;192.168.1.1;123;""
+;eth0;IPv4;junk;_other;local;;;
"""

    def test_parses_known_services(self):
        env = _env()
        obs = mdns_mod.probe(
            env, timeout=1.0,
            runner=lambda services, timeout: self.AVAHI_OUTPUT,
        )
        kinds = sorted(o.kind for o in obs)
        self.assertEqual(kinds, ["kiwisdr", "radiod", "time_source"])
        kiwi = next(o for o in obs if o.kind == "kiwisdr")
        self.assertEqual(kiwi.endpoint, "kiwi1.local:8073")
        self.assertEqual(kiwi.fields["address"], "192.168.1.20")

    def test_missing_avahi_is_soft_failure(self):
        def _raise(services, timeout):
            raise FileNotFoundError("avahi-browse not found on PATH")
        obs = mdns_mod.probe(_env(), runner=_raise)
        self.assertEqual(len(obs), 1)
        self.assertFalse(obs[0].ok)

    def test_disabled_returns_empty(self):
        env = _env()
        env.discovery.mdns_enabled = False
        obs = mdns_mod.probe(env, runner=lambda s, t: self.AVAHI_OUTPUT)
        self.assertEqual(obs, [])


# ---------------------------------------------------------------------------
# Multicast / ka9q-radio status
# ---------------------------------------------------------------------------

class _FakeChannel:
    def __init__(self, freq, preset, rate):
        self.frequency = freq
        self.preset = preset
        self.sample_rate = rate


class _FakeStatus:
    def __init__(self, d): self._d = d
    def to_dict(self): return self._d


class _FakeControl:
    def __init__(self, status_dns, status): self._status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def poll_status(self, ssrc, timeout): return self._status


class MulticastProbeTests(unittest.TestCase):
    def test_emits_channels_and_frontend(self):
        env = _env(radiods=[DeclaredRadiod(
            id='r1', host='bee1.local', status_dns='hf-status.local')])

        def fake_discover(status_dns, listen_duration):
            return {42: _FakeChannel(14_097_000, "wspr", 12_000)}

        def control_factory(status_dns):
            return _FakeControl(status_dns,
                                _FakeStatus({"frontend":
                                             {"lock": True, "calibrate": 0.1}}))

        obs = mcast_mod.probe(env, timeout=1.0,
                              discoverer=fake_discover,
                              control_factory=control_factory)
        self.assertEqual(len(obs), 1)
        self.assertTrue(obs[0].ok)
        self.assertEqual(obs[0].id, 'r1')
        self.assertEqual(len(obs[0].fields["channels"]), 1)
        self.assertTrue(obs[0].fields["frontend"]["gpsdo_lock"])

    def test_discover_error_becomes_failed_obs(self):
        env = _env(radiods=[DeclaredRadiod(
            id='r1', host='bee1.local', status_dns='hf-status.local')])

        def raises(status_dns, listen_duration):
            raise RuntimeError("multicast listen failed")

        obs = mcast_mod.probe(env, timeout=1.0,
                              discoverer=raises, control_factory=None)
        self.assertFalse(obs[0].ok)
        self.assertIn("multicast listen failed", obs[0].error)


# ---------------------------------------------------------------------------
# NTP
# ---------------------------------------------------------------------------

class _FakeSocket:
    """A socket stub that returns one pre-built NTP response."""
    def __init__(self, response: bytes):
        self._response = response
        self._closed = False
    def settimeout(self, t): pass
    def sendto(self, data, addr): pass
    def recvfrom(self, n): return (self._response, ('ref', 123))
    def close(self): self._closed = True


def _build_ntp_response(stratum: int = 2, refid: bytes = b"IP\x01\x01") -> bytes:
    li_vn_mode = (0 << 6) | (4 << 3) | 4       # server mode
    poll = 6
    precision = -20
    root_delay = 65536 * 0.05
    root_dispersion = 65536 * 0.10
    tx_sec = 4_000_000_000
    tx_frac = 0
    return (struct.pack("!B B B b", li_vn_mode, stratum, poll, precision)
            + struct.pack("!I", int(root_delay))
            + struct.pack("!I", int(root_dispersion))
            + refid
            + b"\x00" * 24                         # ref/orig/rx timestamps
            + struct.pack("!II", tx_sec, tx_frac))


class NtpProbeTests(unittest.TestCase):
    def test_parses_remote_response(self):
        env = _env(time_sources=[DeclaredTimeSource(
            id='ntp1', kind='ntp', host='time.example', stratum_max=3)])
        response = _build_ntp_response(stratum=2)
        obs = ntp_mod.probe(env, timeout=1.0,
                            socket_factory=lambda: _FakeSocket(response),
                            chronyc_runner=None)
        self.assertEqual(len(obs), 1)
        self.assertTrue(obs[0].ok)
        self.assertEqual(obs[0].fields["stratum"], 2)
        self.assertEqual(obs[0].fields["mode"], 4)
        self.assertIn('.', obs[0].fields["refid"])

    def test_stratum1_refid_is_ascii(self):
        env = _env(time_sources=[DeclaredTimeSource(
            id='ntp1', kind='ntp', host='time.example')])
        response = _build_ntp_response(stratum=1, refid=b"GPS\x00")
        obs = ntp_mod.probe(env, timeout=1.0,
                            socket_factory=lambda: _FakeSocket(response),
                            chronyc_runner=None)
        self.assertEqual(obs[0].fields["refid"], "GPS")

    def test_localhost_uses_chronyc(self):
        env = _env(time_sources=[DeclaredTimeSource(
            id='ntp-local', kind='ntp', host='localhost')])
        chronyc_csv = (
            "^,*,time.example,2,6,377,10,0.001,0.002,0.0005\n"
            "^,+,time2.example,3,6,255,20,0.010,0.020,0.005\n"
        )
        obs = ntp_mod.probe(env, timeout=1.0,
                            socket_factory=lambda: None,   # not used
                            chronyc_runner=lambda t: chronyc_csv)
        self.assertEqual(len(obs), 1)
        self.assertTrue(obs[0].ok)
        self.assertEqual(len(obs[0].fields["sources"]), 2)
        self.assertEqual(obs[0].fields["stratum"], 2)

    def test_passive_only_skips(self):
        env = _env(time_sources=[DeclaredTimeSource(
            id='ntp1', kind='ntp', host='time.example')])
        env.discovery.passive_only = True
        obs = ntp_mod.probe(env, socket_factory=lambda: _FakeSocket(b"x"))
        self.assertEqual(obs, [])


# ---------------------------------------------------------------------------
# KiwiSDR HTTP
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode() if isinstance(body, str) else body
    def read(self): return self._body


class KiwiProbeTests(unittest.TestCase):
    STATUS = (
        "name=KiwiSDR-Central-IA\n"
        "sw_name=KiwiSDR\n"
        "sw_version=1.752\n"
        "users=3\n"
        "users_max=8\n"
        "gps=52\n"
        "grid=EN34ix\n"
    )
    GPS = json.dumps({"fixes": 52, "fix": True, "lat": 42.0, "lon": -93.0})

    def test_parses_status_and_gps(self):
        env = _env(kiwisdrs=[DeclaredKiwi(
            id='k1', host='kiwi.local', port=8073, gps_expected=True)])

        def fake_urlopen(url, timeout):
            if url.endswith('/status'):
                return _FakeResponse(self.STATUS)
            return _FakeResponse(self.GPS)

        obs = kiwi_mod.probe(env, timeout=1.0, urlopen=fake_urlopen)
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertTrue(o.ok)
        self.assertEqual(o.fields["sw_version"], "1.752")
        self.assertEqual(o.fields["users"], 3)
        self.assertEqual(o.fields["fixes"], 52)
        self.assertTrue(o.fields["gps_fix"])

    def test_status_failure_is_fatal_for_that_kiwi(self):
        env = _env(kiwisdrs=[DeclaredKiwi(id='k1', host='kiwi.local')])

        def fake_urlopen(url, timeout):
            raise OSError("connection refused")

        obs = kiwi_mod.probe(env, timeout=1.0, urlopen=fake_urlopen)
        self.assertFalse(obs[0].ok)
        self.assertIn("/status failed", obs[0].error)

    def test_passive_only_skips(self):
        env = _env(kiwisdrs=[DeclaredKiwi(id='k1', host='kiwi.local')])
        env.discovery.passive_only = True
        obs = kiwi_mod.probe(env, urlopen=lambda u, t: _FakeResponse(""))
        self.assertEqual(obs, [])


# ---------------------------------------------------------------------------
# GPSDO (authority.json)
# ---------------------------------------------------------------------------

class GpsdoProbeTests(unittest.TestCase):
    def test_local_authority_parsed(self):
        env = _env(gpsdos=[DeclaredGpsdo(
            id='g1', kind='leo-bodnar-mini', host='localhost',
            authority_json='/fake/authority.json')])

        sample = json.dumps({
            "locked": True, "sats": 11, "fix_type": "3D",
            "tic_seconds": 1.2e-9, "authority": "bodnar-bee1",
        })
        obs = gpsdo_mod.probe(env, reader=lambda p: sample)
        self.assertEqual(len(obs), 1)
        self.assertTrue(obs[0].ok)
        self.assertTrue(obs[0].fields["locked"])
        self.assertEqual(obs[0].fields["sats"], 11)

    def test_remote_gpsdo_is_skipped(self):
        env = _env(gpsdos=[DeclaredGpsdo(
            id='g2', kind='leo-bodnar-mini', host='bee2.local',
            authority_json='/var/lib/gpsdo-monitor/authority.json')])
        obs = gpsdo_mod.probe(env, reader=lambda p: "")
        self.assertEqual(obs, [])

    def test_missing_file_recorded_as_failure(self):
        env = _env(gpsdos=[DeclaredGpsdo(
            id='g1', kind='leo-bodnar-mini', host='localhost',
            authority_json='/nope')])

        def raise_fnf(p): raise FileNotFoundError(p)
        obs = gpsdo_mod.probe(env, reader=raise_fnf)
        self.assertFalse(obs[0].ok)
        self.assertIn("missing", obs[0].error)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class RateLimiterTests(unittest.TestCase):
    def test_honours_cadence_and_forces_floor(self):
        now = [1000.0]
        rl = RateLimiter(_clock=lambda: now[0])
        self.assertTrue(rl.allow("mdns", "host"))
        now[0] += 10.0                            # inside default 60s cadence
        self.assertFalse(rl.allow("mdns", "host"))
        self.assertTrue(rl.allow("mdns", "host", force=True))
        now[0] += 1.0                             # 1s later, under 5s floor
        self.assertFalse(rl.allow("mdns", "host", force=True))
        now[0] += 5.0                             # past floor
        self.assertTrue(rl.allow("mdns", "host", force=True))

    def test_different_targets_independent(self):
        now = [1000.0]
        rl = RateLimiter(_clock=lambda: now[0])
        self.assertTrue(rl.allow("mdns", "a"))
        self.assertTrue(rl.allow("mdns", "b"))


if __name__ == '__main__':
    unittest.main()
