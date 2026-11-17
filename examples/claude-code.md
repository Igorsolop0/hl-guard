# Connect Claude Code to hl-guard

Claude Code can run shell commands as your user, so it can run
`hl-guard approve` itself and read the agent key. Use it with hl-guard on
testnet only, turn off its shell tool, or run hl-guard under a separate user.
See
[docs/threat-model.md](../docs/threat-model.md).

1. Do steps 1 to 3 of [docs/quickstart.md](../docs/quickstart.md): clone
   hl-guard, write `~/.config/hl-guard/serve.yaml` and approve the agent key.
2. Add the server. Run this in the hl-guard folder, so that `$PWD` is the
   clone:

   ```sh
   claude mcp add --scope user hl-guard -- "$(command -v uv)" run --directory "$PWD" \
     hl-guard serve --config "$HOME/.config/hl-guard/serve.yaml" --expect-network testnet
   ```

3. Close other sessions and clients that run hl-guard, then run
   `claude mcp list`. The `hl-guard` line says `Connected`.
4. Ask Claude "What is the BTC mark price on Hyperliquid testnet?". Claude
   calls the `markets` tool and answers with the mark and mid price from its
   result.

To start from a project file instead, put the same command in `.mcp.json`
with absolute paths:

```json
{
  "mcpServers": {
    "hl-guard": {
      "command": "/path/to/uv",
      "args": ["run", "--directory", "/path/to/hl-guard", "hl-guard", "serve",
               "--config", "/path/to/your-home/.config/hl-guard/serve.yaml",
               "--expect-network", "testnet"]
    }
  }
}
```

With an agent key in the serve config, the server also has the guarded write
tools. Under the example policy every entry waits for `hl-guard approve <id>`.
hl-guard does not place stop-loss or take-profit orders on the exchange.
