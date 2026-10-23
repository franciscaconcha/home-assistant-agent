"""Exercise actual MCP discovery and calls across a stdio subprocess."""

import asyncio
import sys
import os
import tempfile
import json

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
                "yoto_playback", "yoto_set_volume", "ocado_basket", "ocado_next_order",
                "ocado_plan_order_changes", "ocado_apply_order_changes",
                "ocado_weekly_list", "ocado_set_weekly_list", "ocado_weekly_shop",
                "household_list_recipes", "household_get_recipe", "household_save_recipe", "ocado_weekly_plan",
                "ocado_plan_weekly_redo", "ocado_apply_weekly_redo",
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
            disabled = await session.call_tool("ocado_apply_order_changes", {"plan_id": "0" * 32, "confirmed": True})
            assert disabled.isError, disabled
            weekly = await session.call_tool("ocado_weekly_shop", {"trigger": "weekly ocado"})
            assert weekly.isError, weekly
            recipes = await session.call_tool("household_list_recipes", {})
            assert not recipes.isError, recipes
            saved_recipe = await session.call_tool("household_save_recipe", {
                "recipe_id": "smoke-recipe", "name": "Smoke recipe",
                "items": [{"product_id": None, "name": "Unmapped test product", "quantity": 1}],
            })
            assert not saved_recipe.isError, saved_recipe
            draft = await session.call_tool("household_get_recipe", {"name": "Smoke recipe"})
            assert not draft.isError, draft
            value = draft.structuredContent or json.loads(next(block.text for block in draft.content if block.type == "text"))
            assert value["status"] == "needs_products", draft
    print("MCP stdio discovery and household_status passed.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(check(directory))
