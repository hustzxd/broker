import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from broker.broker import ExecutionBroker
from broker import executor
from broker.version import __version__, warn_version_mismatch
from test_cancel_task import MemorySocket


class VersionCheckTests(unittest.IsolatedAsyncioTestCase):
    def test_matching_versions_are_quiet(self):
        logger = Mock()
        warn_version_mismatch(logger, __version__, __version__)
        logger.warning.assert_not_called()

    async def test_broker_warns_but_accepts_mismatch_or_missing_version(self):
        for peer_version in ("0.0.1", None):
            with self.subTest(version=peer_version):
                broker = ExecutionBroker()
                local, remote = MemorySocket(), MemorySocket()
                local.peer, remote.peer = remote, local
                handler = asyncio.create_task(broker.handle_executor(local))
                try:
                    registration = {"type": "register", "executor_id": "test"}
                    if peer_version:
                        registration["version"] = peer_version
                    with self.assertLogs("broker.broker", level="WARNING") as logs:
                        await remote.send(json.dumps(registration))
                        result = json.loads(await asyncio.wait_for(remote.recv(), timeout=5))
                    self.assertTrue(result["accepted"])
                    self.assertEqual(result["version"], __version__)
                    warning = "\n".join(logs.output)
                    self.assertIn(f"broker={__version__}", warning)
                    self.assertIn(f"executor={peer_version or 'unknown'}", warning)
                    self.assertIn("python -m pip install --upgrade git+https://github.com/hustzxd/broker.git", warning)
                    self.assertEqual(broker.list_executors()[0].get("version"), peer_version)
                finally:
                    await remote.close()
                    await asyncio.wait_for(handler, timeout=5)

    async def test_executor_advertises_real_version_and_warns_about_broker(self):
        for peer_version in ("0.0.1", None, __version__):
            with self.subTest(version=peer_version):
                local, remote = MemorySocket(), MemorySocket()
                local.peer, remote.peer = remote, local
                state = {"executor_id": "test", "registered": False, "registered_at": None}
                with patch.object(executor, "logger") as logger:
                    runner = asyncio.create_task(executor._run_executor_connection(
                        remote, "ws://test", state, {"version": "forged"}, False
                    ))
                    try:
                        registration = json.loads(await asyncio.wait_for(local.recv(), timeout=5))
                        self.assertEqual(registration["version"], __version__)
                        await local.send(json.dumps({"accepted": True, "version": peer_version}))
                        await local.messages.put(None)
                        await remote.messages.put(None)
                        await asyncio.wait_for(runner, timeout=5)
                        self.assertTrue(state["registered"])
                        if peer_version == __version__:
                            logger.warning.assert_not_called()
                        else:
                            logger.warning.assert_called_once()
                    finally:
                        runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
