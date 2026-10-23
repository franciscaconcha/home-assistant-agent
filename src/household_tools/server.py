"""Stdio MCP server. Reserve stdout for the MCP protocol."""

from mcp.server.fastmcp import FastMCP
from household_tools.integrations import yoto
from household_tools.integrations import ocado
from household_tools.integrations import ocado_edit
from mcp.types import ToolAnnotations

mcp = FastMCP("household")


@mcp.tool()
def household_status() -> dict[str, str]:
    """Check household tool availability; this does not contact external services."""
    return {
        "server": "ok",
        "yoto": "authorised (not a live check)" if yoto.token_path().exists() else "not authorised",
        "ocado": "session saved (not a live check)" if ocado.session_path().exists() else "not connected",
    }


@mcp.tool()
def yoto_list_players() -> list[dict]:
    """List Yoto players and whether they are online. Does not change playback."""
    return yoto.list_players()


@mcp.tool()
def yoto_player_status(player: str) -> dict:
    """Read live status of one player using its exact operator-configured alias."""
    return yoto.player_status(player)


@mcp.tool()
def yoto_playback(player: str, action: str) -> dict:
    """Pause, resume, or stop one allowed player. Reports delivery, not confirmed execution."""
    return yoto.playback(player, action)


@mcp.tool()
def yoto_set_volume(player: str, volume: int) -> dict:
    """Set integer volume percentage, within the operator's per-player maximum."""
    return yoto.set_volume(player, volume)


@mcp.tool()
def ocado_basket() -> dict:
    """Read live Ocado trolley quantities/prices; names are saved from a Mac browser visit, not live. Cannot change it or check out."""
    return ocado.basket()


@mcp.tool()
def ocado_next_order(reserved_only: bool = True) -> dict:
    """Read the next scheduled Ocado Reserved order, delivery, editability and items. Set reserved_only=false to include one-off orders. Does not open it for editing."""
    return ocado.next_order(reserved_only)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ocado_plan_order_changes(changes: list[dict]) -> dict:
    """Preview changes to existing items in the next Reserved order. Each change has product_id and desired integer quantity (0 removes). Show delivery and before/after quantities, then wait for explicit user confirmation. Preview expires in 10 minutes. No remote edits."""
    return ocado_edit.prepare(changes)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def ocado_apply_order_changes(plan_id: str, confirmed: bool = False) -> dict:
    """Stage a previously previewed order edit ONLY after the user explicitly confirms its delivery and exact changes. Set confirmed=true only then. Never automatically retry after failure. Does not save/check out: user must review and save on Ocado's website."""
    return ocado_edit.apply(plan_id, confirmed)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
