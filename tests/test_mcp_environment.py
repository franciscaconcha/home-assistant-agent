"""Reproduce Hermes's filtered MCP subprocess environment without live credentials."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from household_tools import yoto_setup
from household_tools.integrations import yoto, ocado


class MCPEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_home_survives_filtered_subprocess_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, subprocess_home = Path(directory) / "profile", Path(directory) / "tool-home"
            subprocess_home.mkdir()
            with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
                # Fake marker only; household_status never reads or transmits tokens.
                yoto.write_private(yoto.token_path(), {"test_marker": True})
                yoto.write_private(ocado.session_path(), {"test_marker": True})
                (profile / "config.yaml").write_text("mcp_servers: {}\n")
                with patch.object(yoto, "list_players", return_value=[{
                    "device_id": "test-player", "name": "Test", "online": True,
                }]), patch("builtins.input", side_effect=["1", "test", "30"]):
                    yoto_setup.configure()
                import yaml
                configured = yaml.safe_load((profile / "config.yaml").read_text())

            # Mirror the safe baseline used for MCP processes. The parent home is absent.
            baseline = {key: os.environ[key] for key in ("PATH", "USER", "LANG", "TERM", "SHELL") if key in os.environ}
            baseline["HOME"] = str(subprocess_home)
            for explicit, expected in [(False, "not authorised"), (True, "authorised (not a live check)")]:
                env = dict(baseline)
                if explicit:
                    env.update(configured["mcp_servers"]["household"]["env"])
                server = StdioServerParameters(command=sys.executable,
                    args=["-m", "household_tools.server"], env=env)
                async with stdio_client(server) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("household_status", {})
                        self.assertFalse(result.isError)
                        self.assertEqual(result.structuredContent["yoto"], expected)
                        self.assertEqual(result.structuredContent["ocado"],
                            "session saved (not a live check)" if explicit else "not connected")
