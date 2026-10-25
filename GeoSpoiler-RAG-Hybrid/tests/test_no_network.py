import os
import subprocess
import sys
import unittest
from pathlib import Path


class NoNetworkRuleTests(unittest.TestCase):
    def test_asyncio_socketpair_works_but_direct_connections_stay_blocked(self):
        env = os.environ.copy()
        env["GEOSPOILER_NO_NETWORK"] = "1"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        code = """
import asyncio
import socket
from testing.no_network import NetworkBlockedError
async def task():
    await asyncio.sleep(0)
    return 42
assert asyncio.run(task()) == 42
for address in [('127.0.0.1', 80), ('192.0.2.1', 80)]:
    with socket.socket() as connection:
        try:
            connection.connect(address)
        except NetworkBlockedError:
            pass
        else:
            raise AssertionError('connection escaped guard')
print('passed')
"""
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True,
            text=True, check=True, timeout=30,
        )
        self.assertIn("passed", result.stdout)

    def test_no_network_env_blocks_socket_connections(self):
        env = os.environ.copy()
        env["GEOSPOILER_NO_NETWORK"] = "1"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('example.com', 80), timeout=0.1)\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__)\n"
            "else:\n"
            "    raise SystemExit('network was not blocked')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertIn("NetworkBlockedError", result.stdout)


if __name__ == "__main__":
    unittest.main()
