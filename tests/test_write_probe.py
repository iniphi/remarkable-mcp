r"""rm_health write probe: detecting a write outage that reads cannot see.

On 2026-08-17 the reMarkable cloud began rejecting any root index whose
entries were not sorted by document ID. Every write returned a bare 400 while
every read kept working -- so rm_health, whose every check is a read, reported
"cloud authenticated, ok" throughout a TOTAL write outage and could not have
done otherwise. These tests lock in the opt-in probe that closes that gap.

Run: python -m pytest tests/test_write_probe.py
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = RM_MCP_DIR.parent / "tools"
for _p in (RM_MCP_DIR, TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rm_mcp import device  # noqa: E402


def proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["rmapi"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestWriteProbe(unittest.TestCase):
    def test_success_with_cleanup(self):
        with mock.patch.object(device, "_run", return_value=proc()) as run, \
             mock.patch.object(device, "rm") as rmf:
            out = device.write_probe("/00_Projects", cleanup=True)
        self.assertTrue(out["writable"])
        self.assertTrue(out["cleaned_up"])
        rmf.assert_called_once()
        self.assertEqual(run.call_args[0][0][0], "mkdir")

    def test_probe_path_is_unique_per_call(self):
        with mock.patch.object(device, "_run", return_value=proc()), \
             mock.patch.object(device, "rm"):
            a = device.write_probe("/00_Projects", cleanup=True)["probe_path"]
            b = device.write_probe("/00_Projects", cleanup=True)["probe_path"]
        self.assertNotEqual(a, b)

    def test_probe_path_sits_under_the_given_parent(self):
        with mock.patch.object(device, "_run", return_value=proc()), \
             mock.patch.object(device, "rm"):
            out = device.write_probe("/00_Projects/", cleanup=True)
        self.assertTrue(out["probe_path"].startswith("/00_Projects/.rm_health_probe_"))
        self.assertNotIn("//", out["probe_path"])

    def test_bare_400_is_recognised_as_the_outage_signature(self):
        """The headline case: a bare 400 must name its own cause."""
        with mock.patch.object(device, "_run",
                               return_value=proc(1, stderr="400 invalid root schema")):
            out = device.write_probe("/00_Projects", cleanup=True)
        self.assertFalse(out["writable"])
        self.assertIn("likely_cause", out)
        self.assertIn("f295d54", out["likely_cause"])

    def test_non_400_failure_carries_no_false_diagnosis(self):
        with mock.patch.object(device, "_run",
                               return_value=proc(1, stderr="connection refused")):
            out = device.write_probe("/00_Projects", cleanup=True)
        self.assertFalse(out["writable"])
        self.assertNotIn("likely_cause", out)

    def test_cleanup_refused_reports_the_leftover(self):
        """The hosted lane cannot delete -- litter must be reported, not silent."""
        with mock.patch.object(device, "_run", return_value=proc()), \
             mock.patch.object(device, "rm") as rmf:
            out = device.write_probe("/00_Projects", cleanup=False)
        rmf.assert_not_called()
        self.assertFalse(out["cleaned_up"])
        self.assertIn("RM_MCP_ALLOW_DESTRUCTIVE", out["note"])
        self.assertTrue(out["writable"])

    def test_failed_cleanup_is_reported_not_swallowed(self):
        with mock.patch.object(device, "_run", return_value=proc()), \
             mock.patch.object(device, "rm", side_effect=RuntimeError("nope")):
            out = device.write_probe("/00_Projects", cleanup=True)
        self.assertTrue(out["writable"])
        self.assertFalse(out["cleaned_up"])
        self.assertIn("nope", out["note"])

    def test_mkdir_is_the_only_write_issued_on_success_without_cleanup(self):
        """The probe must write exactly once -- no put, no mv, no extra calls."""
        with mock.patch.object(device, "_run", return_value=proc()) as run,              mock.patch.object(device, "rm"):
            device.write_probe("/00_Projects", cleanup=False)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][0], "mkdir")


class TestHealthProbeMissingRoot(unittest.TestCase):
    """A projects root that does not exist yet must not read as unwritable."""

    def _health(self):
        import asyncio

        from rm_mcp import config as mcp_config
        from rm_mcp import server
        with mock.patch.object(mcp_config, "RMAPI_RESOLVED", "/fake/rmapi"), \
             mock.patch.object(device, "auth_probe",
                               return_value={"authenticated": True}), \
             mock.patch.object(device, "ls",
                               side_effect=RuntimeError("directory doesn't exist")), \
             mock.patch.object(device, "write_probe") as probe:
            result = asyncio.run(server.rm_health(write_probe=True))
        return result, probe

    def test_missing_root_reports_unknown_not_unwritable(self):
        result, _ = self._health()
        data = result["data"]
        self.assertFalse(data["projects_root_exists"])
        self.assertIsNone(data["write_probe"]["writable"])
        self.assertIn("does not exist", data["write_probe"]["detail"])
        self.assertIn("rm_ensure_project_folder", data["write_probe"]["detail"])

    def test_missing_root_issues_no_write(self):
        _, probe = self._health()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
