# Live lanes

Live lanes read Hyperliquid testnet through `hl-guard serve`. The server
enforces the pace, so a lane cannot spend more `/info` weight than its config
allows, however fast the scenario runs.

## Pace

Set `info_weight_per_min: 60` in the lane config. The server charges each
request the weight on the Hyperliquid rate-limit page before it sends it, and
refuses a call with a typed `rate_limited` error when the last minute has no
room for it. All `hl-guard serve` processes that use the same budget file share
one budget. A 429 from the exchange stops every one of them for 60 seconds.

The 1200 per minute that Hyperliquid allows is per IP. Other clients on the
same IP, such as bots or daemons, spend from it too, and the server cannot see
them. Run lanes from a machine where nothing else reads Hyperliquid, or keep
the lane cap well below what those clients leave.

At 60 per minute, `fills` always answers `rate_limited`. The exchange can
return 2000 fills, which costs up to 120 weight. Each lane fits in one minute
except the full read scenario. Wait a minute when a tool answers
`rate_limited`, then run the rest.

## Run a lane

1. Write the lane config next to a copy of `examples/policy.yaml` and run
   `chmod 600` on both files:

   ```yaml
   policy: policy.yaml
   data_dir: data
   account: "0x..."
   info_weight_per_min: 60
   info_budget_file: lanes/info-weight.json
   ```

   Give every lane config the same `info_budget_file`, so that all lanes
   share one budget. Without the key, the server uses the per-user default
   file that `examples/serve.yaml` names.

2. Start the proxy. It tunnels only to the testnet API and logs each tunnel:

   ```sh
   uv run python tests/server/lanes/paced_proxy.py --port 8899 --gap 1 --log proxy.log &
   ```

3. Run the scenario. Put the proxy variables on the server command, after
   `env`. The MCP stdio client in `tools/drive.py` passes only a short list of
   variables (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME`) to the
   server, so a variable set for `drive.py` itself never reaches the server:

   ```sh
   uv run python tools/drive.py examples/scenarios/read.json -- \
     env HTTPS_PROXY=http://127.0.0.1:8899 NO_PROXY= \
     .venv/bin/hl-guard serve --config lane.yaml
   ```

4. Check the spend. `proxy.log` must list one line per request, and
   `lanes/info-weight.json` holds the weight of the last minute.

## Offline lanes

`serve_fake.py` serves recorded answers from a local fake `/info` and charges
no budget. `perf.py` drives it for the overhead probe.
