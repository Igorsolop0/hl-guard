import os
import time

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel

server = MCPServer("echo")


@server.tool()
def echo(text: str) -> str:
    return text


@server.tool()
def fail(reason: str) -> str:
    raise ToolError(reason)


@server.tool()
def sleep(seconds: float) -> str:
    time.sleep(seconds)
    return "woke"


@server.tool()
def pid() -> str:
    return str(os.getpid())


class Answer(BaseModel):
    text: str


@server.tool()
async def ask_client(ctx: Context) -> str:
    await ctx.session.send_ping()
    try:
        await ctx.elicit("answer?", Answer)
    except MCPError as exc:
        return f"client refused: {exc}"
    return "client answered"


if __name__ == "__main__":
    server.run()
