"""Tests for sigmond.sources — per-client SDR source selection model."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

from sigmond.sources import (
    ClientSources,
    InventoryRow,
    SourceKey,
    VALID_SOURCE_TYPES,
    inventory,
)


@dataclass
class _Obs:
    """Minimal stub of sigmond.environment.Observation for inventory()."""
    kind: str
    ok: bool = True
    endpoint: str = ""
    fields: dict = None
    observed_at: float | None = None


# ---------------------------------------------------------------------------
# SourceKey
# ---------------------------------------------------------------------------

class SourceKeyTests(unittest.TestCase):

    def test_round_trip(self):
        k = SourceKey(type="radiod", identifier="bee1-status.local")
        self.assertEqual(str(k), "radiod:bee1-status.local")
        self.assertEqual(SourceKey.parse(str(k)), k)

    def test_kiwisdr_with_port_in_identifier(self):
        k = SourceKey.parse("kiwisdr:192.168.1.20:8073")
        self.assertEqual(k.type, "kiwisdr")
        self.assertEqual(k.identifier, "192.168.1.20:8073")

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            SourceKey(type="bogus", identifier="x")
        with self.assertRaises(ValueError):
            SourceKey.parse("bogus:x")

    def test_rejects_empty_identifier(self):
        with self.assertRaises(ValueError):
            SourceKey(type="radiod", identifier="")

    def test_rejects_no_colon(self):
        with self.assertRaises(ValueError):
            SourceKey.parse("radiod")

    def test_rejects_shell_metacharacters(self):
        for bad in ('foo bar', 'foo;rm -rf', 'foo$BAR', 'foo"x'):
            with self.assertRaises(ValueError, msg=bad):
                SourceKey(type="radiod", identifier=bad)

    def test_hashable_and_equal(self):
        a = SourceKey.parse("radiod:bee1-status.local")
        b = SourceKey.parse("radiod:bee1-status.local")
        c = SourceKey.parse("radiod:bee2-status.local")
        self.assertEqual(a, b)
        self.assertEqual({a, b}, {a})        # hashable
        self.assertNotEqual(a, c)


# ---------------------------------------------------------------------------
# ClientSources I/O
# ---------------------------------------------------------------------------

class ClientSourcesTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_missing_returns_empty(self):
        cs = ClientSources.load("wspr-recorder", root=self.root)
        self.assertEqual(cs.client, "wspr-recorder")
        self.assertEqual(cs.selected, [])

    def test_save_and_load_round_trip(self):
        cs = ClientSources(
            client="wspr-recorder",
            selected=[
                SourceKey.parse("radiod:B4-100-rx888mk2-status.local"),
                SourceKey.parse("radiod:bee1-status.local"),
            ],
        )
        path = cs.save(root=self.root)
        self.assertTrue(path.exists())
        # The file must remain operator-editable plain TOML
        body = path.read_text()
        self.assertIn("selected = [", body)
        self.assertIn("radiod:bee1-status.local", body)

        loaded = ClientSources.load("wspr-recorder", root=self.root)
        self.assertEqual(loaded.selected, cs.selected)

    def test_save_atomic_via_rename(self):
        """A crash mid-write must not corrupt an existing selection."""
        cs = ClientSources(
            client="wspr-recorder",
            selected=[SourceKey.parse("radiod:bee1-status.local")],
        )
        cs.save(root=self.root)
        # No .tmp residue after a successful save
        self.assertFalse(any(self.root.glob("*.tmp")))

    def test_load_rejects_bad_schema(self):
        p = self.root / "wspr-recorder.sources.toml"
        self.root.mkdir(parents=True, exist_ok=True)
        p.write_text('selected = "not-a-list"\n')
        with self.assertRaises(ValueError):
            ClientSources.load("wspr-recorder", root=self.root)

    def test_load_propagates_unknown_source_type(self):
        p = self.root / "wspr-recorder.sources.toml"
        self.root.mkdir(parents=True, exist_ok=True)
        p.write_text('selected = ["bogus:foo"]\n')
        with self.assertRaises(ValueError):
            ClientSources.load("wspr-recorder", root=self.root)

    def test_add_remove_returns_change_flag(self):
        cs = ClientSources(client="wspr-recorder")
        k = SourceKey.parse("radiod:bee1-status.local")
        self.assertTrue(cs.add(k))
        self.assertFalse(cs.add(k))         # idempotent
        self.assertTrue(cs.has(k))
        self.assertTrue(cs.remove(k))
        self.assertFalse(cs.remove(k))      # idempotent

    def test_order_preserved(self):
        cs = ClientSources(client="wspr-recorder")
        for ident in ("a", "b", "c"):
            cs.add(SourceKey(type="radiod", identifier=ident))
        self.assertEqual(
            [k.identifier for k in cs.selected],
            ["a", "b", "c"],
        )


# ---------------------------------------------------------------------------
# inventory() projection
# ---------------------------------------------------------------------------

class InventoryTests(unittest.TestCase):

    def test_radiod_prefers_hostname_over_address(self):
        obs = _Obs(
            kind="radiod",
            endpoint="bee1-status.local:5006",
            fields={
                "mdns_name": "AC0G @EM38ww B1 T3FD",
                "address":   "239.205.73.40",
            },
            observed_at=1.0,
        )
        [row] = inventory([obs])
        self.assertEqual(row.key, SourceKey.parse("radiod:bee1-status.local"))
        self.assertEqual(row.label, "AC0G @EM38ww B1 T3FD")
        self.assertEqual(row.reachability, "ok")

    def test_radiod_falls_back_to_address_when_no_hostname(self):
        obs = _Obs(
            kind="radiod",
            endpoint="",
            fields={"address": "239.205.73.40"},
        )
        [row] = inventory([obs])
        self.assertEqual(row.key, SourceKey.parse("radiod:239.205.73.40"))

    def test_kiwisdr_uses_endpoint(self):
        obs = _Obs(
            kind="kiwisdr",
            endpoint="kiwi1.local:8073",
            fields={"mdns_name": "kiwi-east"},
        )
        [row] = inventory([obs])
        self.assertEqual(row.key, SourceKey.parse("kiwisdr:kiwi1.local:8073"))
        self.assertEqual(row.label, "kiwi-east")

    def test_dedupe_same_key(self):
        obs1 = _Obs(kind="radiod", endpoint="bee1-status.local:5006",
                    fields={"mdns_name": "first"})
        obs2 = _Obs(kind="radiod", endpoint="bee1-status.local:5006",
                    fields={"mdns_name": "later — should not replace"})
        rows = inventory([obs1, obs2])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].label, "first")

    def test_skips_unknown_kinds(self):
        obs_known = _Obs(kind="radiod", endpoint="bee1-status.local:5006",
                         fields={"mdns_name": "kept"})
        obs_other = _Obs(kind="gpsdo", endpoint="leo.local:80")
        rows = inventory([obs_known, obs_other])
        self.assertEqual([r.key.type for r in rows], ["radiod"])

    def test_skips_failed_observations(self):
        obs = _Obs(kind="radiod", ok=False, endpoint="bee1-status.local:5006",
                   fields={"mdns_name": "x"})
        self.assertEqual(inventory([obs]), [])


if __name__ == "__main__":
    unittest.main()
