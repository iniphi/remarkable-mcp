"""auth_probe distinguishes a cold or busy instance from a lapsed pairing.

Fully offline: rm_config.run_rmapi is mocked, no rmapi process is ever run.
Additive contract: "authenticated" and "detail" keep their meaning; a new
"state" key is one of ok / unauthenticated / cold / busy.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from rm_mcp import device
from rm_mcp.device import rm_config


def _proc(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["rmapi"], returncode, stdout, stderr)


class AuthProbeStateTests(unittest.TestCase):
    def _probe(self, **kw):
        with mock.patch.object(rm_config, "run_rmapi", **kw):
            return device.auth_probe()

    def test_success_is_ok(self) -> None:
        probe = self._probe(return_value=_proc(0, "[d] x"))
        self.assertIs(probe["authenticated"], True)
        self.assertEqual(probe["detail"], "ok")
        self.assertEqual(probe["state"], "ok")

    def test_unauthenticated_is_false(self) -> None:
        probe = self._probe(return_value=_proc(1, "", "401 unauthorized"))
        self.assertIs(probe["authenticated"], False)
        self.assertEqual(probe["state"], "unauthenticated")

    def test_timeout_is_cold_not_unauthenticated(self) -> None:
        probe = self._probe(side_effect=subprocess.TimeoutExpired("rmapi", 30))
        self.assertIsNone(probe["authenticated"])
        self.assertEqual(probe["state"], "cold")
        self.assertIn("cold instance", probe["detail"])
        self.assertIn("retry", probe["detail"])
        self.assertIn("timed out", probe["detail"])

    def test_busy_lock_is_busy(self) -> None:
        probe = self._probe(side_effect=rm_config.RmapiBusyError("lock held"))
        self.assertIsNone(probe["authenticated"])
        self.assertEqual(probe["state"], "busy")
        self.assertIn("lock held", probe["detail"])
        self.assertIn("retry", probe["detail"])


if __name__ == "__main__":
    unittest.main()
