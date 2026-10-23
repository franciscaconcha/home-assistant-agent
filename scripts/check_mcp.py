"""Exercise actual MCP discovery and calls across a stdio subprocess."""

import asyncio
import sys
import os
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(directory: str) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "household_tools.server"],
        env={**os.environ, "HERMES_HOME": directory},
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "household_status", "yoto_list_players", "yoto_player_status",
                "yoto_playback", "yoto_set_volume", "ocado_basket",
            }
            result = await session.call_tool("household_status", {})
            assert not result.isError, result
            assert result.structuredContent == {
                "server": "ok", "yoto": "not authorised", "ocado": "not connected"
            }, result
            denied = await session.call_tool("yoto_playback", {"player": "missing", "action": "pause"})
            assert denied.isError, denied
            missing = await session.call_tool("ocado_basket", {})
            assert missing.isError, missing
    print("MCP stdio discovery and household_status passed.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(check(directory))
