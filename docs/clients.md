# Connect a client

hl-guard is a stdio MCP server. The client starts it with
`hl-guard serve --config <serve config>` and talks to it over standard input
and output. This page shows the setup for Claude Desktop, Claude Code and
Cursor.

Do steps 1 to 4 of the [quick start](quickstart.md) first. You need the
hl-guard clone, `~/.config/hl-guard/serve.yaml` and, for the write tools, an
approved agent key. For read tools only, remove the `agent_key_file` line
from the config and skip step 3. After you connect a client here, go on with
[step 6 of the quick start](quickstart.md#6-watch-the-policy-reject-an-order).

Only one `hl-guard serve` with write tools can run per account, so connect
one client at a time. A second server refuses to start with
`another hl-guard serve with write tools holds …; stop it first`. This
includes `tools/drive.py` below. If you set up another client before, remove
or turn off its hl-guard entry first.

> [!WARNING]
> Claude Code and Cursor can run shell commands as your user. Such a client
> can run `hl-guard approve` itself and read the agent key, so it can approve
> its own orders. Use them with hl-guard only on testnet, or turn off their
> shell tools, or run hl-guard under a separate user as in
> [separate-user.md](separate-user.md). See
> [threat-model.md](threat-model.md).

## Fill in the paths

Clients do not expand `~` and often do not read your shell's `PATH`, so the
config needs absolute paths. Run this in the hl-guard folder. It prints the
config block with the paths of your machine:

```sh
cat <<EOF
{
  "mcpServers": {
    "hl-guard": {
      "command": "$(command -v uv)",
      "args": ["run", "--directory", "$PWD", "hl-guard", "serve",
               "--config", "$HOME/.config/hl-guard/serve.yaml",
               "--expect-network", "testnet"]
    }
  }
}
EOF
```

[examples/claude-desktop.json](../examples/claude-desktop.json) and
[examples/cursor.json](../examples/cursor.json) hold the same block with
placeholder paths. Replace each `/path/to/…` with an absolute path.

`--expect-network testnet` makes the server refuse to start if the data dir
says mainnet. For mainnet, see [policy.md](policy.md#mainnet).

## Claude Desktop

1. Open **Settings**, then **Developer**, then **Edit Config**. This opens
   `claude_desktop_config.json`.
2. Paste the block. If the file already has an `mcpServers` object, add only
   the `hl-guard` entry to it.
3. Quit Claude Desktop completely and open it again.
4. Open the tools menu in a new chat. It lists the hl-guard tools.

If the tools do not appear, open **Settings**, then **Developer**. The
hl-guard entry shows the error. A line that starts with
`hl-guard serve refused:` names the problem and the fix.

## Claude Code

For testing, start one session with a config of its own. It does not add
hl-guard to your other Claude Code sessions, which would each start their own
`hl-guard serve` and clash on the one-server lock. Save the block from
[Fill in the paths](#fill-in-the-paths) as a file outside the repository, for
example `~/.config/hl-guard/claude-code-mcp.json`, and start Claude Code with
it:

<!-- docs-test: skip client -->
```sh
claude --mcp-config ~/.config/hl-guard/claude-code-mcp.json --strict-mcp-config
```

`--strict-mcp-config` makes the session use only the servers in that file.
The file has this shape:

```json
{
  "mcpServers": {
    "hl-guard": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/hl-guard", "hl-guard", "serve",
               "--config", "/absolute/path/to/your-home/.config/hl-guard/serve.yaml",
               "--expect-network", "testnet"]
    }
  }
}
```

In the session, `/mcp` lists hl-guard and its tools.

To add the server to every Claude Code session for your user instead, run
this in the hl-guard folder:

<!-- docs-test: skip client -->
```sh
claude mcp add --scope user hl-guard -- "$(command -v uv)" run --directory "$PWD" \
  hl-guard serve --config "$HOME/.config/hl-guard/serve.yaml" --expect-network testnet
```

Check it:

<!-- docs-test: skip client -->
```sh
claude mcp list
```

The `hl-guard` line says `Connected`. `claude mcp list` starts its own
`hl-guard serve` for the check, so close other Claude Code sessions and
clients that run hl-guard first. A failure that names `serve.lock` means
another server is running. In a session, `/mcp` lists the tools.

To share the setup in a project instead, put the block from
[Fill in the paths](#fill-in-the-paths) in a `.mcp.json` file at the project
root. [examples/claude-code.md](../examples/claude-code.md) has the same
steps.

## Cursor

1. Open **Cursor Settings**, then **MCP**, then **Add new global MCP server**.
   This opens `~/.cursor/mcp.json`. For one project only, use
   `.cursor/mcp.json` in the project folder.
2. Paste the block. If the file already has an `mcpServers` object, add only
   the `hl-guard` entry to it.
3. Save the file. In **Cursor Settings**, then **MCP**, the hl-guard server
   shows a green dot and lists its tools.

## Check the connection

Ask the model:

> What is the BTC mark price on Hyperliquid testnet?

It calls the `markets` tool and answers with the mark and mid price. Then
ask:

> Show my hl-guard risk status.

It calls `risk_status` and shows your limits, entries today and whether new
entries are blocked.

With an agent key, the client lists 13 tools: 8 read tools and the write
tools `propose_order`, `close_position`, `cancel_order`, `halt` and
`proposal_status`. Without one, it lists the 8 read tools only.

## Test without a client

`tools/drive.py` runs a list of tool calls against the server and prints the
whole MCP exchange. It needs the testnet:

<!-- docs-test: skip network -->
```sh
uv run python tools/drive.py examples/scenarios/read.json -- \
  uv run hl-guard serve --config ~/.config/hl-guard/serve.yaml --expect-network testnet
```
