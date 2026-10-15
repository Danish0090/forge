"""Host-side MCP server: lets an MCP client use tools the ladder has approved. Needs the optional `mcp` extra."""

try:
    from forge.mcpserver.server import ForgeMcp
except ImportError as exc:  # pragma: no cover - only without the extra
    raise ImportError("MCP support needs the optional extra: uv sync --extra mcp") from exc

__all__ = ["ForgeMcp"]
