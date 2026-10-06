"""A name conflict on the device is reported as one, not as a cloud fault.

rmapi's "entry already exists" means the target name is taken on the device.
Found live 2026-10-06 (full-function test of the public build): a second
rm_push_image with the same title came back system=cloud with "check
device/cloud reachability with rm_health, then retry" -- and a retry fails
identically, forever, so an agent following that remedy loops.

Offline: no device, no network. Ships in the public tree with the code it tests.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import envelope  # noqa: E402

MSG = ("rmapi put Notes.pdf -> /Projects/notes failed: ERROR: 2026/10/06 16:29:56 "
       "main.go:86: Error:  entry already exists (use --force to recreate, "
       "--content-only to replace content)")


class TestNameConflictReattributed(unittest.TestCase):
    def test_conflict_is_a_device_error_with_a_rename_remedy(self):
        res = envelope.err_from_exception(
            RuntimeError(MSG), "cloud",
            "check device/cloud reachability with rm_health, then retry")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["system"], "device")
        self.assertIn("already exists", res["error"]["message"])
        remedy = res["error"]["remedy"]
        self.assertNotIn("then retry", remedy)
        self.assertNotIn("--force", remedy)
        self.assertIn("title", remedy)
        self.assertIn("rm_delete", remedy)

    def test_other_runtime_errors_keep_the_callers_attribution(self):
        res = envelope.err_from_exception(RuntimeError("connection reset"),
                                          "cloud", "retry later")
        self.assertEqual(res["error"]["system"], "cloud")
        self.assertEqual(res["error"]["remedy"], "retry later")


if __name__ == "__main__":
    unittest.main()
