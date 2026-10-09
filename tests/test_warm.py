"""rm_mcp.warm: the cold-start rmapi tree warm step.

Offline. run_rmapi is always mocked; the real rmapi and the reMarkable cloud
are never touched. The contract under test: ONE `ls /` through run_rmapi with
a long timeout, every failure becomes one stderr line, the exit status is
always 0 so a failed warm can never stop the server starting.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import warm  # noqa: E402
from rm_mcp.config import rm_config  # noqa: E402

ENTRYPOINT = RM_MCP_DIR / "docker-entrypoint.sh"


def _proc(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["rmapi"], rc, out, err)


class WarmTest(unittest.TestCase):
    def run_main(self, env: dict | None = None, **patch_kw):
        env = {"RM_WARM_DISABLE": "", "RM_WARM_TIMEOUT_S": "", **(env or {})}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(warm.tree_cache, "restore",
                                  return_value="kept local"), \
                mock.patch.object(warm.tree_cache, "save",
                                  return_value="unchanged"), \
                mock.patch.object(rm_config, "run_rmapi", **patch_kw) as run, \
                mock.patch("sys.stderr") as err:
            code = warm.main()
        lines = "".join(c.args[0] for c in err.write.call_args_list)
        return code, run, lines

    def test_success_one_call_default_timeout(self) -> None:
        code, run, log = self.run_main(return_value=_proc())
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("ls", "/"))
        self.assertEqual(run.call_args.kwargs["timeout"], 150.0)
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertIn("ok", log)
        self.assertEqual(len([x for x in log.splitlines() if x.strip()]), 3)

    def test_timeout_exits_zero_and_logs(self) -> None:
        code, _, log = self.run_main(
            side_effect=subprocess.TimeoutExpired("rmapi", 200))
        self.assertEqual(code, 0)
        self.assertIn("timed out", log)

    def test_throttled_exits_zero(self) -> None:
        exc = rm_config.RmapiThrottledError("429", time.time() + 300)
        code, _, log = self.run_main(side_effect=exc)
        self.assertEqual(code, 0)
        self.assertIn("throttled", log)

    def test_busy_lock_exits_zero(self) -> None:
        code, _, log = self.run_main(side_effect=rm_config.RmapiBusyError("busy"))
        self.assertEqual(code, 0)
        self.assertIn("busy", log)

    def test_missing_binary_exits_zero(self) -> None:
        code, _, log = self.run_main(
            side_effect=rm_config.RmapiNotFoundError("no rmapi"))
        self.assertEqual(code, 0)
        self.assertIn("not found", log)

    def test_nonzero_exit_exits_zero(self) -> None:
        code, _, log = self.run_main(return_value=_proc(1, "", "boom"))
        self.assertEqual(code, 0)
        self.assertIn("exit 1", log)

    def test_unexpected_exception_exits_zero(self) -> None:
        code, _, log = self.run_main(side_effect=RuntimeError("weird"))
        self.assertEqual(code, 0)
        self.assertIn("weird", log)

    def test_disable_never_calls_rmapi(self) -> None:
        code, run, log = self.run_main(env={"RM_WARM_DISABLE": "1"})
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("disabled", log)

    def test_timeout_env_respected(self) -> None:
        _, run, _ = self.run_main(env={"RM_WARM_TIMEOUT_S": "45"},
                                  return_value=_proc())
        self.assertEqual(run.call_args.kwargs["timeout"], 45.0)

    def test_bad_timeout_env_falls_back(self) -> None:
        _, run, _ = self.run_main(env={"RM_WARM_TIMEOUT_S": "abc"},
                                  return_value=_proc())
        self.assertEqual(run.call_args.kwargs["timeout"], 150.0)


class WarmOrderTest(unittest.TestCase):
    """restore -> ls -> save (only after a successful ls)."""

    def run_main(self, ls):
        order = []

        def fake_ls(*a, **k):
            order.append("ls")
            return ls()

        with mock.patch.dict("os.environ", {"RM_WARM_DISABLE": "",
                                            "RM_WARM_TIMEOUT_S": ""}),                 mock.patch.object(warm.tree_cache, "restore",
                                  side_effect=lambda: order.append("restore")
                                  or "kept local"),                 mock.patch.object(warm.tree_cache, "save",
                                  side_effect=lambda: order.append("save")
                                  or "saved"),                 mock.patch.object(rm_config, "run_rmapi", side_effect=fake_ls),                 mock.patch("sys.stderr") as err:
            code = warm.main()
        log = "".join(c.args[0] for c in err.write.call_args_list)
        return code, order, log

    def test_order_on_success(self) -> None:
        code, order, log = self.run_main(lambda: _proc())
        self.assertEqual(code, 0)
        self.assertEqual(order, ["restore", "ls", "save"])
        self.assertIn("restore", log)
        self.assertIn("save", log)

    def test_no_save_after_timeout(self) -> None:
        def boom():
            raise subprocess.TimeoutExpired("rmapi", 150)
        code, order, _ = self.run_main(boom)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["restore", "ls"])

    def test_no_save_after_nonzero(self) -> None:
        _, order, _ = self.run_main(lambda: _proc(1, "", "x"))
        self.assertEqual(order, ["restore", "ls"])

    def test_restore_or_save_crash_never_blocks(self) -> None:
        with mock.patch.dict("os.environ", {"RM_WARM_DISABLE": ""}),                 mock.patch.object(warm.tree_cache, "restore",
                                  side_effect=RuntimeError("r")),                 mock.patch.object(warm.tree_cache, "save",
                                  side_effect=RuntimeError("s")),                 mock.patch.object(rm_config, "run_rmapi",
                                  return_value=_proc()),                 mock.patch("sys.stderr"):
            self.assertEqual(warm.main(), 0)

    def test_disable_skips_restore_too(self) -> None:
        with mock.patch.dict("os.environ", {"RM_WARM_DISABLE": "1"}),                 mock.patch.object(warm.tree_cache, "restore") as r,                 mock.patch("sys.stderr"):
            warm.main()
        r.assert_not_called()


@unittest.skipUnless(ENTRYPOINT.is_file(), "cloud lane only: not shipped publicly")
class EntrypointTest(unittest.TestCase):
    def test_warm_runs_after_conf_before_exec(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        conf = text.index("base64 -d")
        warm_at = text.index("python -m rm_mcp.warm")
        serve = text.index("exec python run_server.py")
        self.assertLess(conf, warm_at)
        self.assertLess(warm_at, serve)

    def test_warm_line_is_not_exec_and_ignores_status(self) -> None:
        line = next(x for x in ENTRYPOINT.read_text(encoding="utf-8").splitlines()
                    if "rm_mcp.warm" in x and not x.lstrip().startswith("#"))
        self.assertNotIn("exec", line)
        self.assertIn("|| true", line)


if __name__ == "__main__":
    unittest.main()
