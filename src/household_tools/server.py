"""Stdio MCP server. Reserve stdout for the MCP protocol."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("household")


@mcp.tool()
def household_status() -> dict[str, str]:
    """Check household tool availability; this does not contact external services."""
    return {
        "server": "ok",
        "yoto": "not implemented",
        "ocado": "not implemented",
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
