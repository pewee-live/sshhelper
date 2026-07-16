"""Tests for baseline.py: snapshot storage and drift diff."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
from baseline import BaselineManager, BASELINE_MANAGER
import shutil


class TestBaselineDiff:
    """Configuration drift detection."""

    def setup_method(self):
        self.mgr = BaselineManager()
        self.device = "test-diff-device"

    def teardown_method(self):
        d = os.path.join("data", "baselines", self.mgr._safe_name(self.device))
        if os.path.exists(d):
            shutil.rmtree(d)

    def test_save_and_list(self):
        self.mgr.save_snapshot(self.device, {"ip_addr": "1: lo\n2: eth0 inet 10.0.0.1"})
        snaps = self.mgr.list_snapshots(self.device)
        assert len(snaps) >= 1
        assert snaps[0]["probe_count"] == 1

    def test_diff_detects_changes(self):
        self.mgr.save_snapshot(self.device, {"ip": "10.0.0.1", "route": "default via 10.0.0.254"})
        self.mgr.save_snapshot(self.device, {"ip": "10.0.0.2", "route": "default via 10.0.0.254"})
        diff = self.mgr.diff(self.device)
        assert diff is not None
        assert diff["changed_probes"] == 1
        details = diff["details"]
        assert "ip" in details
        assert "10.0.0.1" in str(details["ip"]["removed"])
        assert "10.0.0.2" in str(details["ip"]["added"])

    def test_diff_no_changes(self):
        self.mgr.save_snapshot(self.device, {"ip": "10.0.0.1"})
        self.mgr.save_snapshot(self.device, {"ip": "10.0.0.1"})
        diff = self.mgr.diff(self.device)
        assert diff["changed_probes"] == 0

    def test_diff_insufficient_snapshots(self):
        self.mgr.save_snapshot(self.device, {"ip": "10.0.0.1"})
        diff = self.mgr.diff(self.device)
        assert diff is None

    def test_format_diff(self):
        self.mgr.save_snapshot(self.device, {"iptables": "Chain INPUT (policy ACCEPT)"})
        self.mgr.save_snapshot(self.device, {"iptables": "Chain INPUT (policy DROP)"})
        diff = self.mgr.diff(self.device)
        text = BaselineManager.format_diff(diff)
        assert "DROP" in text
        assert "ACCEPT" in text
        assert "CONFIG DRIFT" in text

    def test_rapid_snapshots_not_clobbered(self):
        """Two snapshots saved in the same second should not overwrite each other."""
        snap1 = self.mgr.save_snapshot(self.device, {"a": "1"})
        snap2 = self.mgr.save_snapshot(self.device, {"a": "2"})
        snaps = self.mgr.list_snapshots(self.device)
        assert len(snaps) >= 2