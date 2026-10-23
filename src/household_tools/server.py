"""Stdio MCP server. Reserve stdout for the MCP protocol."""

from mcp.server.fastmcp import FastMCP
from household_tools.integrations import yoto

mcp = FastMCP("household")


@mcp.tool()
def household_status() -> dict[str, str]:
    """Check household tool availability; this does not contact external services."""
    return {
        "server": "ok",
        "yoto": "authorised (not a live check)" if yoto.token_path().exists() else "not authorised",
        "ocado": "not implemented",
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
