"""Tests for the local-resources probe.

All external transports (read_proc, run_ethtool, read_dmesg, snapshot
store) are injected so the probe runs offline with no subprocess and
no /proc access.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.discovery import local_resources as lr
from sigmond.environment import Environment, DeclaredLocalSystem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROC_STAT_T0 = """\
cpu  1000 0 200 8000 50 0 100 0 0 0
cpu0 200 0 50 1900 10 0 25 0 0 0
cpu1 250 0 50 1950 10 0 25 0 0 0
cpu2 250 0 50 1950 15 0 25 0 0 0
cpu3 300 0 50 2200 15 0 25 0 0 0
intr 12345
ctxt 67890
"""

# Same set, advanced by 100 jiffies on cpu0 (mostly user load) and
# 100 on cpu1 (mostly softirq load).
PROC_STAT_T1 = """\
cpu  1200 0 200 8200 50 0 200 0 0 0
cpu0 280 0 50 1960 10 0 25 0 0 0
cpu1 250 0 50 1950 10 0 125 0 0 0
cpu2 250 0 50 1950 15 0 25 0 0 0
cpu3 300 0 50 2200 15 0 25 0 0 0
"""

PROC_NET_SNMP_T0 = """\
Ip: Forwarding DefaultTTL InReceives ...
Ip: 1 64 12345
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti
Udp: 1000 5 2 800 10 0 0 0
"""

# 60 seconds later: RcvbufErrors +30, InErrors +1.
PROC_NET_SNMP_T1 = """\
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti
Udp: 2000 5 3 1500 40 0 0 0
"""

PROC_INTERRUPTS = """\
           CPU0       CPU1       CPU2       CPU3
  0:         45          0          0          0   IO-APIC    2-edge      timer
130:     123456     654321          0          0   IR-PCI-MSI 0-edge      xhci_hcd
131:        100        200          0          0   IR-PCI-MSI 0-edge      xhci_hcd
140:          0          0       1283          0   IR-PCI-MSI 0-edge      eth0-rx
"""

ETHTOOL_OUT = """\
NIC statistics:
     rx_packets: 12345
     tx_packets: 6789
     rx_missed_errors: 0
     rx_no_buffer_count: 4
     rx_fifo_errors: 0
     rx_dropped: 0
     multicast: 1234
     other_unrelated_stat: 99
"""

DMESG_OUT = """\
[12345.001] usb 3-1: new SuperSpeed USB device number 5 using xhci_hcd
[12346.002] xhci_hcd 0000:00:14.0: urb error -71
[12347.003] xhci_hcd 0000:00:14.0: USB disconnect, reset device
[12348.004] xhci_hcd 0000:00:14.0: urb error -32
"""


def _make_env(**overrides) -> Environment:
    ls = DeclaredLocalSystem(**overrides)
    return Environment(local_system=ls)


def _proc_reader(stat: str = PROC_STAT_T0,
                 snmp: str = PROC_NET_SNMP_T0,
                 ints: str = PROC_INTERRUPTS):
    """Return a callable matching probe()'s read_proc signature."""
    table = {
        "/proc/stat": stat,
        "/proc/net/snmp": snmp,
        "/proc/interrupts": ints,
    }
    return lambda path: table.get(path, "")


# ---------------------------------------------------------------------------
# /proc/stat parsing + delta
# ---------------------------------------------------------------------------

class ProcStatTests(unittest.TestCase):
    def test_per_core_only(self):
        out = lr._parse_proc_stat(PROC_STAT_T0)
        self.assertEqual(set(out.keys()),
                         {"cpu0", "cpu1", "cpu2", "cpu3"})

    def test_field_layout(self):
        out = lr._parse_proc_stat(PROC_STAT_T0)
        self.assertEqual(out["cpu0"]["user"], 200)
        self.assertEqual(out["cpu0"]["system"], 50)
        self.assertEqual(out["cpu0"]["idle"], 1900)
        self.assertEqual(out["cpu0"]["softirq"], 25)

    def test_short_rows_padded(self):
        out = lr._parse_proc_stat("cpu0 100 0 50 800")
        self.assertEqual(out["cpu0"]["user"], 100)
        self.assertEqual(out["cpu0"]["idle"], 800)
        self.assertEqual(out["cpu0"]["softirq"], 0)

    def test_aggregate_skipped(self):
        out = lr._parse_proc_stat("cpu 100 0 50 800\ncpu0 50 0 25 400")
        self.assertNotIn("cpu", out)
        self.assertIn("cpu0", out)


class DeltaCpuTests(unittest.TestCase):
    def test_first_run_zeros(self):
        cur = lr._parse_proc_stat(PROC_STAT_T0)
        out = lr._delta_cpu({}, cur)
        self.assertEqual(len(out), 4)
        # First-run path returns 0 for all rates.
        for row in out:
            self.assertEqual(row["usr"], 0.0)
            self.assertEqual(row["soft"], 0.0)
            self.assertEqual(row["total_jiffies"], 0)

    def test_user_load_on_cpu0(self):
        prev = lr._parse_proc_stat(PROC_STAT_T0)
        cur = lr._parse_proc_stat(PROC_STAT_T1)
        out = lr._delta_cpu(prev, cur)
        cpu0 = next(r for r in out if r["core"] == 0)
        # cpu0 went user 200→280 (+80), idle 1900→1960 (+60). Total 140.
        # %usr ≈ 57, %idle ≈ 43.
        self.assertEqual(cpu0["total_jiffies"], 140)
        self.assertGreater(cpu0["usr"], 50.0)
        self.assertLess(cpu0["soft"], 5.0)

    def test_softirq_load_on_cpu1(self):
        prev = lr._parse_proc_stat(PROC_STAT_T0)
        cur = lr._parse_proc_stat(PROC_STAT_T1)
        out = lr._delta_cpu(prev, cur)
        cpu1 = next(r for r in out if r["core"] == 1)
        # cpu1 softirq 25→125 (+100), idle 1950→1950 (+0). Total 100.
        self.assertEqual(cpu1["total_jiffies"], 100)
        self.assertGreater(cpu1["soft"], 90.0)


# ---------------------------------------------------------------------------
# /proc/net/snmp UDP parsing + delta
# ---------------------------------------------------------------------------

class ProcNetSnmpTests(unittest.TestCase):
    def test_extracts_udp_row(self):
        out = lr._parse_proc_net_snmp_udp(PROC_NET_SNMP_T0)
        self.assertEqual(out["RcvbufErrors"], 10)
        self.assertEqual(out["InErrors"], 2)
        self.assertEqual(out["InDatagrams"], 1000)

    def test_missing_udp_section_returns_empty(self):
        self.assertEqual(lr._parse_proc_net_snmp_udp("Tcp: foo\nTcp: 1"), {})


class DeltaUdpTests(unittest.TestCase):
    def test_first_run_rates_zero_totals_present(self):
        cur = lr._parse_proc_net_snmp_udp(PROC_NET_SNMP_T0)
        out = lr._delta_udp({}, cur, 0.0)
        self.assertEqual(out["rcvbuf_errors_total"], 10)
        self.assertEqual(out["rcvbuf_errors_rate"], 0.0)

    def test_rate_over_interval(self):
        prev = lr._parse_proc_net_snmp_udp(PROC_NET_SNMP_T0)
        cur = lr._parse_proc_net_snmp_udp(PROC_NET_SNMP_T1)
        # +30 RcvbufErrors over 60s = 0.5 / s.
        out = lr._delta_udp(prev, cur, 60.0)
        self.assertEqual(out["rcvbuf_errors_total"], 40)
        self.assertEqual(out["rcvbuf_errors_rate"], 0.5)
        self.assertEqual(out["in_errors_rate"], round(1 / 60.0, 4))

    def test_negative_delta_clamped(self):
        # Counter wrapped or daemon restarted: prev > cur.  Don't emit
        # a negative rate.
        out = lr._delta_udp({"RcvbufErrors": 100},
                            {"RcvbufErrors": 10}, 60.0)
        self.assertEqual(out["rcvbuf_errors_rate"], 0.0)


class SnakeCaseTests(unittest.TestCase):
    def test_camel_to_snake(self):
        self.assertEqual(lr._snake("RcvbufErrors"), "rcvbuf_errors")
        self.assertEqual(lr._snake("InCsumErrors"), "in_csum_errors")


# ---------------------------------------------------------------------------
# /proc/interrupts parsing + summary
# ---------------------------------------------------------------------------

class ProcInterruptsTests(unittest.TestCase):
    def test_single_handler_summed_across_lines(self):
        # xhci_hcd has two lines: 130 (123456, 654321) and 131 (100, 200).
        out = lr._parse_proc_interrupts(PROC_INTERRUPTS, ["xhci_hcd"])
        self.assertEqual(out["xhci_hcd"], [123556, 654521, 0, 0])

    def test_unknown_handler_yields_zeros(self):
        out = lr._parse_proc_interrupts(PROC_INTERRUPTS, ["does_not_exist"])
        self.assertEqual(out["does_not_exist"], [0, 0, 0, 0])

    def test_empty_handler_set_returns_empty(self):
        self.assertEqual(lr._parse_proc_interrupts(PROC_INTERRUPTS, []), {})

    def test_no_cpu_header_returns_empty(self):
        self.assertEqual(
            lr._parse_proc_interrupts("not the format we expect", ["x"]), {}
        )


class SummariseIrqTests(unittest.TestCase):
    def test_observed_cores_match_nonzero(self):
        cur = {"xhci_hcd": [123556, 654521, 0, 0]}
        out = lr._summarise_irq(cur, {"xhci_hcd": [2, 3]})
        self.assertEqual(out["xhci_hcd"]["expected_cores"], [2, 3])
        self.assertEqual(out["xhci_hcd"]["observed_cores"], [0, 1])
        self.assertEqual(out["xhci_hcd"]["per_core_count"],
                         [123556, 654521, 0, 0])

    def test_handler_with_no_declaration(self):
        out = lr._summarise_irq({"foo": [0, 0, 5]}, {})
        self.assertEqual(out["foo"]["expected_cores"], [])
        self.assertEqual(out["foo"]["observed_cores"], [2])


# ---------------------------------------------------------------------------
# ethtool parsing
# ---------------------------------------------------------------------------

class EthtoolTests(unittest.TestCase):
    def test_keeps_only_known_counters(self):
        out = lr._parse_ethtool(ETHTOOL_OUT)
        self.assertEqual(set(out.keys()), set(lr._NIC_COUNTERS))
        self.assertEqual(out["rx_no_buffer_count"], 4)
        self.assertEqual(out["multicast"], 1234)
        self.assertNotIn("rx_packets", out)
        self.assertNotIn("other_unrelated_stat", out)

    def test_empty_output(self):
        self.assertEqual(lr._parse_ethtool(""), {})


# ---------------------------------------------------------------------------
# dmesg USB tally
# ---------------------------------------------------------------------------

class DmesgUsbTests(unittest.TestCase):
    def test_counts_each_pattern(self):
        out = lr._parse_dmesg_usb(DMESG_OUT, ["1d50:6150"], 60)
        # Two "urb error" lines, one "USB ... reset" line, no overruns.
        self.assertEqual(out["urb_errors"], 2)
        self.assertEqual(out["resets"], 1)
        self.assertEqual(out["overruns"], 0)
        self.assertEqual(out["window_seconds"], 60)
        self.assertEqual(out["watched_devices"], ["1d50:6150"])

    def test_quiet_dmesg(self):
        out = lr._parse_dmesg_usb("", ["1d50:6150"], 60)
        self.assertEqual(out["urb_errors"], 0)
        self.assertEqual(out["resets"], 0)
        self.assertEqual(out["overruns"], 0)


# ---------------------------------------------------------------------------
# Full probe() integration
# ---------------------------------------------------------------------------

class ProbeIntegrationTests(unittest.TestCase):
    def _run_probe(self, *, env, prev=None, t1=PROC_STAT_T1,
                   snmp=PROC_NET_SNMP_T1, clock_value=160.0):
        """Run probe with canned transports.  Returns (obs, captured_snap)."""
        captured: list = []

        def save(source, snap):
            captured.append((source, snap))

        obs = lr.probe(
            env,
            timeout=1.0,
            read_proc=_proc_reader(stat=t1, snmp=snmp),
            run_ethtool=lambda iface, t: ETHTOOL_OUT,
            read_dmesg=lambda secs, t: DMESG_OUT,
            load_prev=lambda src: prev,
            save_curr=save,
            clock=lambda: clock_value,
        )
        return obs, captured

    def test_first_run_emits_one_observation_with_all_fields(self):
        env = _make_env(
            nics=["eth0"], usb_devices=["1d50:6150"],
            irq_pins={"xhci_hcd": [2, 3]},
        )
        obs, _ = self._run_probe(env=env, prev=None,
                                 t1=PROC_STAT_T0, snmp=PROC_NET_SNMP_T0)
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual(o.source, "local_resources")
        self.assertEqual(o.kind, "local_system")
        self.assertEqual(o.id, "localhost")
        self.assertTrue(o.ok)
        self.assertEqual(set(o.fields.keys()),
                         {"cpu_per_core", "udp", "nics", "irqs", "usb"})

    def test_first_run_interval_is_zero(self):
        env = _make_env(nics=["eth0"])
        obs, _ = self._run_probe(env=env, prev=None,
                                 t1=PROC_STAT_T0, snmp=PROC_NET_SNMP_T0)
        self.assertEqual(obs[0].fields["udp"]["interval_s"], 0.0)
        self.assertEqual(obs[0].fields["udp"]["rcvbuf_errors_rate"], 0.0)

    def test_second_run_uses_prev_snapshot_for_rate(self):
        env = _make_env(nics=["eth0"])
        prev = {
            "captured_at": 100.0,
            "cpu": lr._parse_proc_stat(PROC_STAT_T0),
            "udp": lr._parse_proc_net_snmp_udp(PROC_NET_SNMP_T0),
            "irq": {},
        }
        obs, _ = self._run_probe(env=env, prev=prev, clock_value=160.0)
        udp = obs[0].fields["udp"]
        self.assertEqual(udp["interval_s"], 60.0)
        self.assertEqual(udp["rcvbuf_errors_rate"], 0.5)  # +30 over 60s

    def test_empty_nics_skips_ethtool(self):
        env = _make_env()  # no nics declared
        ethtool_called: list = []

        lr.probe(
            env,
            read_proc=_proc_reader(),
            run_ethtool=lambda i, t: ethtool_called.append(i) or "",
            read_dmesg=lambda s, t: "",
            load_prev=lambda src: None,
            save_curr=lambda src, snap: None,
            clock=lambda: 0.0,
        )
        self.assertEqual(ethtool_called, [])

    def test_empty_usb_devices_skips_dmesg(self):
        env = _make_env(nics=["eth0"])  # no usb_devices declared
        dmesg_called: list = []

        lr.probe(
            env,
            read_proc=_proc_reader(),
            run_ethtool=lambda i, t: "",
            read_dmesg=lambda s, t: dmesg_called.append(s) or "",
            load_prev=lambda src: None,
            save_curr=lambda src, snap: None,
            clock=lambda: 0.0,
        )
        self.assertEqual(dmesg_called, [])

    def test_proc_read_failure_recorded_in_errors(self):
        env = _make_env(nics=["eth0"])

        def failing_reader(path):
            if path == "/proc/stat":
                raise OSError("simulated permission denied")
            return _proc_reader()(path)

        obs = lr.probe(
            env,
            read_proc=failing_reader,
            run_ethtool=lambda i, t: ETHTOOL_OUT,
            read_dmesg=lambda s, t: "",
            load_prev=lambda src: None,
            save_curr=lambda src, snap: None,
            clock=lambda: 0.0,
        )
        self.assertFalse(obs[0].ok)
        self.assertIn("/proc/stat", obs[0].error)

    def test_snapshot_persisted(self):
        env = _make_env(nics=["eth0"])
        _, captured = self._run_probe(env=env, prev=None)
        self.assertEqual(len(captured), 1)
        source, snap = captured[0]
        self.assertEqual(source, "local_resources")
        self.assertIn("captured_at", snap)
        self.assertIn("cpu", snap)
        self.assertIn("udp", snap)


class DispatchRegistrationTests(unittest.TestCase):
    """Confirm the source is reachable through the discovery dispatch
    table — catches typos in __init__.py registration."""

    def test_module_for_source_resolves(self):
        from sigmond import discovery
        mod = discovery.module_for_source("local_resources")
        self.assertIsNotNone(mod)
        self.assertTrue(hasattr(mod, "probe"))

    def test_targets_for_source_returns_localhost(self):
        from sigmond import discovery
        env = _make_env()
        self.assertEqual(
            discovery.targets_for_source("local_resources", env),
            ["localhost"],
        )

    def test_listed_in_active_sources(self):
        from sigmond import discovery
        self.assertIn("local_resources", discovery.ACTIVE_SOURCES)
        self.assertIn("local_resources", discovery.ALL_SOURCES)
        self.assertNotIn("local_resources", discovery.PASSIVE_SOURCES)

    def test_passive_only_excludes_local_resources(self):
        from sigmond import discovery
        from sigmond.environment import DiscoveryCfg
        env = _make_env()
        env.discovery = DiscoveryCfg(passive_only=True)
        sources = discovery.resolve_sources(env, None)
        self.assertNotIn("local_resources", sources)

    def test_default_cadence_present(self):
        from sigmond import discovery
        self.assertIn("local_resources", discovery.DEFAULT_CADENCE)
        self.assertGreater(discovery.DEFAULT_CADENCE["local_resources"], 0)


if __name__ == '__main__':
    unittest.main()
