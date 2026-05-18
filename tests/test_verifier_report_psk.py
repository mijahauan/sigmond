"""Tests for sigmond.commands.verifier_report_psk (Phase 2 PR 7).

We don't touch the network or ClickHouse — query_psk_server's HTTP
call is faked, and the local sink db is a temp sqlite seeded inline.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.commands.verifier_report_psk import (
    LocalRow, UpstreamResult, Cohorts, CycleStats,
    SpotKey, _row_to_key, read_local_rows,
    classify, cadence_stats, _split_userinfo,
    _build_psk_query, _resolve_urls,
    _parse_window, _detect_default_rx_call,
    DEFAULT_IN_FLIGHT_WINDOW_SEC,
    DEFAULT_WD_URLS,
)


UTC = timezone.utc


# ── window parsing (small DSL reused from wspr-side verifier_report) ────────

class TestParseWindow(unittest.TestCase):

    def test_minutes(self):
        self.assertEqual(_parse_window("30m"), timedelta(minutes=30))

    def test_hours(self):
        self.assertEqual(_parse_window("2h"), timedelta(hours=2))

    def test_days(self):
        self.assertEqual(_parse_window("7d"), timedelta(days=7))

    def test_unrecognized_raises(self):
        with self.assertRaises(ValueError):
            _parse_window("forever")


# ── _row_to_key: SpotKey construction from a psk.spots payload ──────────────

class TestRowToKey(unittest.TestCase):

    def test_full_payload(self):
        payload = {
            "time": "2026-05-18T14:30:15+00:00",
            "mode": "ft8",
            "tx_call": "K1ABC",
            "frequency": 14_074_580,
            "rx_sign": "AC0G/B1",
        }
        key = _row_to_key(payload)
        self.assertIsNotNone(key)
        epoch, mode, tx, freq = key
        self.assertEqual(mode, "ft8")
        self.assertEqual(tx, "K1ABC")
        self.assertEqual(freq, 14_074_580)
        # Epoch is seconds-since-1970 (UTC).
        expected_epoch = int(datetime(2026, 5, 18, 14, 30, 15,
                                      tzinfo=UTC).timestamp())
        self.assertEqual(epoch, expected_epoch)

    def test_z_suffix_time(self):
        key = _row_to_key({"time": "2026-05-18T14:30:15Z", "mode": "ft8",
                           "tx_call": "K1ABC", "frequency": 14_074_580})
        self.assertIsNotNone(key)

    def test_missing_required_returns_none(self):
        # Various incomplete payloads — each must skip cleanly.
        for bad in (
            {"time": "2026-05-18T14:30:15Z", "mode": "ft8",
             "tx_call": "K1ABC"},                       # no freq
            {"time": "2026-05-18T14:30:15Z", "mode": "",
             "tx_call": "K1ABC", "frequency": 14_074_580},   # no mode
            {"time": "2026-05-18T14:30:15Z", "mode": "ft8",
             "tx_call": "", "frequency": 14_074_580},   # no tx_call
            {"time": "garbage", "mode": "ft8",
             "tx_call": "K1ABC", "frequency": 14_074_580},
            {"mode": "ft8", "tx_call": "K1ABC",
             "frequency": 14_074_580},                  # no time
        ):
            self.assertIsNone(_row_to_key(bad), bad)

    def test_tx_call_uppercased_mode_lowered(self):
        key = _row_to_key({"time": "2026-05-18T14:30:15Z", "mode": "FT8",
                           "tx_call": "k1abc", "frequency": 14_074_580})
        self.assertEqual(key[1], "ft8")
        self.assertEqual(key[2], "K1ABC")


# ── read_local_rows: filtering by window + forward flag + rx_sign ──────────

def _seed_db(path: str, rows):
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute(
        "CREATE TABLE pending_uploads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, target_db TEXT, "
        "target_table TEXT, schema_version INTEGER, payload_json TEXT, "
        "queued_at TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO pending_uploads(target_db, target_table, "
            "schema_version, payload_json, queued_at) VALUES (?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


def _payload(**overrides) -> str:
    p = dict(
        time="2026-05-18T14:30:15+00:00", mode="ft8",
        tx_call="K1ABC", frequency=14_074_580,
        rx_sign="AC0G/B1", forward_to_pskreporter=True,
    )
    p.update(overrides)
    return json.dumps(p)


class TestReadLocalRows(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.path = self.tmp.name
        # 3 forward=true rows, 1 forward=false row, all within window.
        # queued_at intentionally non-uniform so the cohort split is
        # exercised by classify() later.
        rows = [
            ("psk", "spots", 2, _payload(time="2026-05-18T14:30:15Z",
                                          tx_call="K1ABC"),
             "2026-05-18T14:30:20+00:00"),
            ("psk", "spots", 2, _payload(time="2026-05-18T14:30:30Z",
                                          tx_call="W1XYZ"),
             "2026-05-18T14:30:35+00:00"),
            ("psk", "spots", 2, _payload(time="2026-05-18T14:30:45Z",
                                          tx_call="N0VFJ"),
             "2026-05-18T14:30:50+00:00"),
            # forward=false: must be excluded.
            ("psk", "spots", 2, _payload(time="2026-05-18T14:30:15Z",
                                          tx_call="K9XX",
                                          forward_to_pskreporter=False),
             "2026-05-18T14:30:25+00:00"),
            # Out-of-window: queued before --window.
            ("psk", "spots", 2, _payload(time="2026-05-18T13:30:15Z",
                                          tx_call="OLD"),
             "2026-05-18T13:30:20+00:00"),
            # Different rx_sign — must be filtered out when rx_sign is set.
            ("psk", "spots", 2, _payload(time="2026-05-18T14:30:15Z",
                                          tx_call="W1XYZ",
                                          rx_sign="K9XYZ/A"),
             "2026-05-18T14:30:55+00:00"),
            # WSPR side — must be ignored regardless.
            ("wspr", "spots", 3, json.dumps({"time": "2026-05-18T14:30:00Z"}),
             "2026-05-18T14:30:00+00:00"),
        ]
        _seed_db(self.path, rows)

    def tearDown(self):
        Path(self.path).unlink(missing_ok=True)

    def test_window_filter_excludes_old_rows(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            rows = read_local_rows(conn,
                                   since_iso="2026-05-18T14:00:00+00:00",
                                   rx_sign="AC0G/B1")
        finally:
            conn.close()
        tx_calls = sorted(r.tx_call for r in rows)
        # Forward-only AND rx_sign='AC0G/B1' AND in-window:
        self.assertEqual(tx_calls, ["K1ABC", "N0VFJ", "W1XYZ"])

    def test_rx_sign_filter(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            rows = read_local_rows(conn,
                                   since_iso="2026-05-18T14:00:00+00:00",
                                   rx_sign="K9XYZ/A")
        finally:
            conn.close()
        self.assertEqual([r.tx_call for r in rows], ["W1XYZ"])

    def test_forward_only_default_excludes_forward_false(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            rows = read_local_rows(conn,
                                   since_iso="2026-05-18T14:00:00+00:00",
                                   rx_sign=None)
        finally:
            conn.close()
        tx_calls = {r.tx_call for r in rows}
        self.assertNotIn("K9XX", tx_calls)

    def test_forward_only_off_includes_all(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            rows = read_local_rows(conn,
                                   since_iso="2026-05-18T14:00:00+00:00",
                                   rx_sign=None,
                                   forward_only=False)
        finally:
            conn.close()
        self.assertIn("K9XX", {r.tx_call for r in rows})


# ── classify: cohort assignment ─────────────────────────────────────────────

class TestClassify(unittest.TestCase):

    def _row(self, *, key_epoch, tx, queued_at):
        return LocalRow(
            key=(key_epoch, "ft8", tx, 14_074_580),
            queued_at=queued_at, rx_sign="AC0G/B1", mode="ft8",
            tx_call=tx, frequency=14_074_580,
        )

    def test_delivered_when_in_upstream(self):
        now = datetime(2026, 5, 18, 14, 35, tzinfo=UTC)
        rows = [self._row(key_epoch=1747577415, tx="K1ABC",
                          queued_at=now - timedelta(minutes=10))]
        upstream = frozenset({(1747577415, "ft8", "K1ABC", 14_074_580)})
        cohorts = classify(rows, upstream, now=now)
        self.assertEqual(len(cohorts.delivered), 1)
        self.assertEqual(len(cohorts.lost), 0)
        self.assertEqual(len(cohorts.in_flight), 0)

    def test_in_flight_when_recent_and_absent(self):
        now = datetime(2026, 5, 18, 14, 35, tzinfo=UTC)
        # queued just now; not in upstream — within in-flight window.
        rows = [self._row(key_epoch=1747577415, tx="K1ABC",
                          queued_at=now - timedelta(seconds=30))]
        cohorts = classify(rows, frozenset(), now=now)
        self.assertEqual(len(cohorts.in_flight), 1)
        self.assertEqual(len(cohorts.lost), 0)

    def test_lost_when_old_and_absent(self):
        now = datetime(2026, 5, 18, 14, 35, tzinfo=UTC)
        # queued well outside the in-flight window; not in upstream.
        rows = [self._row(key_epoch=1747577415, tx="K1ABC",
                          queued_at=now - timedelta(seconds=DEFAULT_IN_FLIGHT_WINDOW_SEC + 60))]
        cohorts = classify(rows, frozenset(), now=now)
        self.assertEqual(len(cohorts.lost), 1)
        self.assertEqual(len(cohorts.in_flight), 0)

    def test_custom_in_flight_window(self):
        """Operator can shrink the window so 'should have arrived by now'
        kicks in faster on a quiet host."""
        now = datetime(2026, 5, 18, 14, 35, tzinfo=UTC)
        rows = [self._row(key_epoch=1747577415, tx="K1ABC",
                          queued_at=now - timedelta(seconds=120))]
        # Default (300s): in_flight. Custom 60s: lost.
        c_default = classify(rows, frozenset(), now=now)
        c_tight   = classify(rows, frozenset(), now=now,
                             in_flight_window_sec=60)
        self.assertEqual(len(c_default.in_flight), 1)
        self.assertEqual(len(c_tight.lost), 1)

    def test_classify_rejects_none_in_flight_sec(self):
        """A None in_flight_window_sec must blow up immediately, not
        silently — caller dispatch should resolve None → module default.
        Regression for the argparse-None-as-default bug observed during
        B4-100 rollout 2026-05-18.
        """
        now = datetime(2026, 5, 18, 14, 35, tzinfo=UTC)
        rows = [self._row(key_epoch=1, tx="K", queued_at=now)]
        with self.assertRaises(TypeError):
            classify(rows, frozenset(), now=now, in_flight_window_sec=None)


# ── cadence_stats ───────────────────────────────────────────────────────────

class TestCadenceStats(unittest.TestCase):

    def test_expected_cycles_for_window(self):
        since = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
        until = datetime(2026, 5, 18, 14, 15, tzinfo=UTC)
        # 15 minutes = 60 FT8 cycles, 120 FT4 cycles.
        stats = cadence_stats([], since=since, until=until)
        by_mode = {s.mode: s for s in stats}
        self.assertEqual(by_mode["ft8"].expected_cycles, 60)
        self.assertEqual(by_mode["ft4"].expected_cycles, 120)

    def test_counts_distinct_cycle_buckets(self):
        since = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
        until = datetime(2026, 5, 18, 14, 1, tzinfo=UTC)
        # All four spots in the SAME 15s bucket → 1 cycle with data,
        # 4 expected cycles in the minute, 3 zero-decode cycles.
        rows = []
        for i in range(4):
            rows.append(LocalRow(
                key=(int(since.timestamp()) + 2 + i, "ft8", f"K{i}", 14_074_580),
                queued_at=since, rx_sign="AC0G/B1", mode="ft8",
                tx_call=f"K{i}", frequency=14_074_580,
            ))
        stats = cadence_stats(rows, since=since, until=until)
        ft8 = next(s for s in stats if s.mode == "ft8")
        self.assertEqual(ft8.expected_cycles, 4)
        self.assertEqual(ft8.cycles_with_data, 1)
        self.assertEqual(ft8.cycles_zero, 3)
        self.assertEqual(ft8.total_spots, 4)


# ── _split_userinfo + _build_psk_query + _resolve_urls ──────────────────────

class TestUrlHelpers(unittest.TestCase):

    def test_split_userinfo_present(self):
        clean, user, pw = _split_userinfo("http://u:p@wd10.example.org")
        self.assertEqual(clean, "http://wd10.example.org")
        self.assertEqual(user, "u")
        self.assertEqual(pw, "p")

    def test_split_userinfo_absent(self):
        clean, user, pw = _split_userinfo("http://wd10.example.org")
        self.assertEqual(clean, "http://wd10.example.org")
        self.assertIsNone(user)
        self.assertIsNone(pw)

    def test_split_userinfo_preserves_port(self):
        clean, user, pw = _split_userinfo("http://u:p@wd10.example.org:8123")
        self.assertEqual(clean, "http://wd10.example.org:8123")

    def test_build_psk_query_includes_reporter(self):
        sql = _build_psk_query("AC0G/B1", 30)
        self.assertIn("rx_sign='AC0G/B1'", sql)
        self.assertIn("INTERVAL 30 MINUTE", sql)
        self.assertIn("now('UTC')", sql)  # TZ-safe
        self.assertIn("FROM psk.spots", sql)

    def test_build_psk_query_omits_reporter_when_none(self):
        sql = _build_psk_query(None, 60)
        self.assertNotIn("rx_sign=", sql)

    def test_resolve_urls_arg_wins(self):
        urls = _resolve_urls("http://a,http://b")
        self.assertEqual(urls, ["http://a", "http://b"])

    def test_resolve_urls_strips_blanks(self):
        urls = _resolve_urls("http://a , ,http://b,")
        self.assertEqual(urls, ["http://a", "http://b"])

    def test_resolve_urls_default(self):
        # No arg, clear env so the default is what comes back.
        import os as _os
        original = _os.environ.pop("WSPRDAEMON_VERIFY_URLS", None)
        try:
            urls = _resolve_urls(None)
            self.assertEqual(urls, [
                "http://wd10.wsprdaemon.org",
                "http://wd20.wsprdaemon.org",
                "http://wd30.wsprdaemon.org",
            ])
        finally:
            if original is not None:
                _os.environ["WSPRDAEMON_VERIFY_URLS"] = original


# ── _detect_default_rx_call ─────────────────────────────────────────────────

class TestDetectDefaultRxCall(unittest.TestCase):

    def test_picks_most_common(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            path = f.name
        try:
            _seed_db(path, [
                ("psk", "spots", 2, _payload(rx_sign="AC0G/B1"),
                 "2026-05-18T14:00:00+00:00"),
                ("psk", "spots", 2, _payload(rx_sign="AC0G/B1"),
                 "2026-05-18T14:01:00+00:00"),
                ("psk", "spots", 2, _payload(rx_sign="AC0G/B1"),
                 "2026-05-18T14:02:00+00:00"),
                ("psk", "spots", 2, _payload(rx_sign="K9XYZ"),
                 "2026-05-18T14:03:00+00:00"),
            ])
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertEqual(_detect_default_rx_call(conn), "AC0G/B1")
            finally:
                conn.close()
        finally:
            Path(path).unlink(missing_ok=True)

    def test_empty_table_returns_none(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            path = f.name
        try:
            _seed_db(path, [])
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertIsNone(_detect_default_rx_call(conn))
            finally:
                conn.close()
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
