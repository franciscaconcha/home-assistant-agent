"""Exercise actual MCP discovery and calls across a stdio subprocess."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check() -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "household_tools.server"],
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {"household_status"}
            result = await session.call_tool("household_status", {})
            assert not result.isError, result
            assert result.structuredContent == {
                "server": "ok", "yoto": "not implemented", "ocado": "not implemented"
            }, result
    print("MCP stdio discovery and household_status passed.")


if __name__ == "__main__":
    asyncio.run(check())
