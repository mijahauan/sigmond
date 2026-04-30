"""Tests for the local_system expect-classifier (Phase 4).

Two layers:
  1. Direct unit tests on the classifier function (fast, exhaustive).
  2. End-to-end through reconcile() to confirm the KindSpec wiring is
     correct and the Delta status reaches the consumer.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.environment import (
    DeclaredLocalSystem,
    Environment,
    Observation,
)
from sigmond.environment_kinds import _local_system_classifier
from sigmond.discovery.reconciler import reconcile


def _ls(**kw) -> DeclaredLocalSystem:
    return DeclaredLocalSystem(**kw)


def _obs(fields: dict) -> Observation:
    return Observation(
        source="local_resources", kind="local_system", id="localhost",
        endpoint="localhost", fields=fields, observed_at=100.0, ok=True,
    )


# ---------------------------------------------------------------------------
# Direct classifier tests
# ---------------------------------------------------------------------------

class NoExpectKeysTests(unittest.TestCase):
    """An operator who declared local_system only for the probe to
    *collect* metrics (without thresholds) should never see degraded."""

    def test_empty_expect_is_healthy(self):
        status, _ = _local_system_classifier(_ls(), [_obs({"udp": {"rcvbuf_errors_rate": 99.9}})])
        self.assertEqual(status, "healthy")

    def test_no_observations_is_healthy(self):
        status, _ = _local_system_classifier(
            _ls(expect={"udp_rcvbuf_errors_rate_max": 0}), [],
        )
        self.assertEqual(status, "healthy")


class UdpRateTests(unittest.TestCase):
    def test_rate_above_max_is_degraded(self):
        ls = _ls(expect={"udp_rcvbuf_errors_rate_max": 0})
        status, detail = _local_system_classifier(
            ls, [_obs({"udp": {"rcvbuf_errors_rate": 0.5}})],
        )
        self.assertEqual(status, "degraded")
        self.assertIn("rcvbuf_errors_rate", detail)
        self.assertIn("0.5", detail)

    def test_rate_at_max_is_healthy(self):
        # Exact equality is healthy — operator's max is inclusive.
        ls = _ls(expect={"udp_rcvbuf_errors_rate_max": 1.0})
        status, _ = _local_system_classifier(
            ls, [_obs({"udp": {"rcvbuf_errors_rate": 1.0}})],
        )
        self.assertEqual(status, "healthy")

    def test_in_errors_rate_threshold(self):
        ls = _ls(expect={"udp_in_errors_rate_max": 0})
        status, detail = _local_system_classifier(
            ls, [_obs({"udp": {"in_errors_rate": 0.1}})],
        )
        self.assertEqual(status, "degraded")
        self.assertIn("in_errors_rate", detail)


class SoftirqTests(unittest.TestCase):
    def test_busy_core_above_max_is_degraded(self):
        ls = _ls(expect={"softirq_percent_max": 30})
        cores = [
            {"core": 0, "soft": 5},
            {"core": 1, "soft": 45},
            {"core": 2, "soft": 10},
        ]
        status, detail = _local_system_classifier(
            ls, [_obs({"cpu_per_core": cores})],
        )
        self.assertEqual(status, "degraded")
        self.assertIn("softirq", detail)
        self.assertIn("45", detail)

    def test_all_cores_below_max_is_healthy(self):
        ls = _ls(expect={"softirq_percent_max": 30})
        cores = [{"core": i, "soft": 5} for i in range(4)]
        status, _ = _local_system_classifier(
            ls, [_obs({"cpu_per_core": cores})],
        )
        self.assertEqual(status, "healthy")


class IrqDriftTests(unittest.TestCase):
    def test_drift_detected_when_disallowed(self):
        ls = _ls(
            irq_pins={"xhci_hcd": [2, 3]},
            expect={"irq_pin_drift_allowed": False},
        )
        irqs = {
            "xhci_hcd": {
                "expected_cores": [2, 3],
                "observed_cores": [2, 10],   # core 10 is the drift
                "per_core_count": [],
            },
        }
        status, detail = _local_system_classifier(ls, [_obs({"irqs": irqs})])
        self.assertEqual(status, "degraded")
        self.assertIn("xhci_hcd", detail)
        self.assertIn("10", detail)

    def test_drift_allowed_stays_healthy(self):
        ls = _ls(
            irq_pins={"xhci_hcd": [2, 3]},
            expect={"irq_pin_drift_allowed": True},
        )
        irqs = {
            "xhci_hcd": {
                "expected_cores": [2, 3],
                "observed_cores": [2, 10],
            },
        }
        status, _ = _local_system_classifier(ls, [_obs({"irqs": irqs})])
        self.assertEqual(status, "healthy")

    def test_no_expected_cores_is_healthy(self):
        # If the operator declared no irq_pins for a handler, drift is
        # undefined — don't degrade on it.
        ls = _ls(expect={"irq_pin_drift_allowed": False})
        irqs = {"xhci_hcd": {"expected_cores": [], "observed_cores": [10]}}
        status, _ = _local_system_classifier(ls, [_obs({"irqs": irqs})])
        self.assertEqual(status, "healthy")

    def test_observed_subset_of_expected_is_healthy(self):
        # Handler fires on core 2 only; expected set was [2, 3].
        # Subset is fine — no drift.
        ls = _ls(
            irq_pins={"xhci_hcd": [2, 3]},
            expect={"irq_pin_drift_allowed": False},
        )
        irqs = {"xhci_hcd": {"expected_cores": [2, 3], "observed_cores": [2]}}
        status, _ = _local_system_classifier(ls, [_obs({"irqs": irqs})])
        self.assertEqual(status, "healthy")


class FirstFailureWinsTests(unittest.TestCase):
    """Order matters when multiple expectations fail: udp rate first,
    then softirq, then drift.  Diagnostic-value ordering."""

    def test_udp_failure_reported_before_softirq(self):
        ls = _ls(expect={
            "udp_rcvbuf_errors_rate_max": 0,
            "softirq_percent_max": 30,
        })
        cores = [{"core": 0, "soft": 99}]   # would trigger softirq alone
        fields = {
            "udp": {"rcvbuf_errors_rate": 1.0},
            "cpu_per_core": cores,
        }
        status, detail = _local_system_classifier(ls, [_obs(fields)])
        self.assertEqual(status, "degraded")
        self.assertIn("rcvbuf", detail)
        self.assertNotIn("softirq", detail)


# ---------------------------------------------------------------------------
# Reconciler integration — proves the KindSpec wiring is live
# ---------------------------------------------------------------------------

class ReconcilerWiringTests(unittest.TestCase):
    def test_degraded_delta_surfaces_through_reconcile(self):
        env = Environment(local_system=_ls(
            expect={"udp_rcvbuf_errors_rate_max": 0},
        ))
        obs = _obs({"udp": {"rcvbuf_errors_rate": 0.5}})
        deltas = reconcile(env, [obs])
        ls_deltas = [d for d in deltas if d.kind == "local_system"]
        self.assertEqual(len(ls_deltas), 1)
        self.assertEqual(ls_deltas[0].status, "degraded")
        self.assertIn("rcvbuf", ls_deltas[0].detail)

    def test_healthy_delta_when_under_threshold(self):
        env = Environment(local_system=_ls(
            expect={"udp_rcvbuf_errors_rate_max": 1.0},
        ))
        obs = _obs({"udp": {"rcvbuf_errors_rate": 0.1}})
        deltas = reconcile(env, [obs])
        ls_deltas = [d for d in deltas if d.kind == "local_system"]
        self.assertEqual(len(ls_deltas), 1)
        self.assertEqual(ls_deltas[0].status, "healthy")

    def test_undeclared_local_system_emits_no_delta(self):
        # Operator hasn't put anything in [local_system] — iter_filter
        # skips it, so reconcile should not yield a local_system delta
        # even when an observation arrives.
        env = Environment()
        obs = _obs({"udp": {"rcvbuf_errors_rate": 0.5}})
        deltas = reconcile(env, [obs])
        # The observation will become an "unknown-extra" delta, not a
        # declared healthy/degraded one.
        ls_declared = [d for d in deltas
                       if d.kind == "local_system"
                       and d.status in ("healthy", "degraded", "missing")]
        self.assertEqual(ls_declared, [])


if __name__ == '__main__':
    unittest.main()
