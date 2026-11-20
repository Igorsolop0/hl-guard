# Write a policy

The policy is a YAML file with your limits. hl-guard reads it once when
`hl-guard serve` starts. No MCP tool can change it or reload it. A change
takes effect only when you restart the server.

Start from a copy of [examples/policy.yaml](../examples/policy.yaml), as in
step 2 of the [quick start](quickstart.md), and check every change to it:

```sh
uv run hl-guard policy check ~/.config/hl-guard/policy.yaml
```

`policy check` prints `policy … ok` and one line per limit with its value and
its product hard ceiling or a note. A value outside a ceiling, an unknown key or a
missing key fails the check with exit code 2, and `hl-guard serve` refuses to
start with the same message.

## Where to keep it

Keep the policy and the serve config outside any folder that an AI tool can
write, for example in `~/.config/hl-guard/`. hl-guard refuses a policy or a
config file that group or others can write or that another user owns, and a
policy in a world-writable folder without the sticky bit:

```sh
chmod 700 ~/.config/hl-guard
chmod 600 ~/.config/hl-guard/policy.yaml ~/.config/hl-guard/serve.yaml
```

hl-guard cannot stop a process that runs as your user from editing the file.
The edit takes effect at the next start. See [threat-model.md](threat-model.md).

## The limits

Every key below is required unless it has a default. A number is a plain
decimal, such as `3600` or `0.5`. hl-guard refuses YAML anchors, octal or hex
numbers, and duplicate keys.

### What can be traded

| Key | Meaning | Example | Ceiling |
|---|---|---|---|
| `network` | `testnet` or `mainnet`. It must match the data dir mode, see [Mainnet](#mainnet). | `testnet` | |
| `coins` | The perp coins that entries may use, as Hyperliquid names them. | `[BTC, ETH, SOL]` | at least one |
| `leverage_max` | Highest isolated leverage of an entry. Default 1. | `1` | 5 |

Entries are isolated margin only. When you hold no position in the coin,
hl-guard sets the coin to isolated at the proposal's leverage before it sends
the entry. When you hold one, the entry must use the position's leverage.

### Size and exposure

| Key | Meaning | Example | Ceiling |
|---|---|---|---|
| `per_order_usd_max` | Most USD notional of one entry. It must not be above `total_exposure_usd_max`. | `20` | 5000 |
| `total_exposure_usd_max` | Most USD of open positions plus resting opening orders, after the entry. | `100` | 20000 |
| `open_orders_max` | Most resting opening orders. | `3` | 20 |
| `entries_per_day_max` | Most entries sent per UTC day. | `2` | 50 |
| `cooldown_s` | Seconds after an entry in a coin before the next entry in that coin. | `3600` | at least 60 |

The exchange refuses an order below 10 USD, so hl-guard rejects such an entry
before it sends it. An entry counts toward `entries_per_day_max` and the
cooldown once the exchange accepts it, even when it is cancelled unfilled
later. An order that the exchange refuses does not count.

### Loss limits

| Key | Meaning | Example | Ceiling |
|---|---|---|---|
| `daily_loss_pct_max` | Percent of the day's starting equity that may be lost in one UTC day. | `3` | 10 |
| `loss_streak_max` | Positions in a row that orders of hl-guard closed at a loss. | `5` | 10 |

When the realized loss of the day, or the drop of the account value from the
day start, reaches `daily_loss_pct_max`, or the loss streak reaches
`loss_streak_max`, hl-guard writes a latch record to the ledger. While the
account is latched, every entry is rejected. Closes and `halt` still pass.

The realized loss and the loss streak count only the fills of orders
hl-guard placed. A stop or a close that you place in the Hyperliquid app
does not count toward them. Its loss still counts through the drop of the
account value, but it never adds to the loss streak.

Only you can clear the latch, with a command that no tool can run:

<!-- docs-test: skip needs-ledger -->
```sh
uv run hl-guard latch clear ~/.hl-guard/ledger.jsonl
```

### Stop-loss and take-profit

| Key | Meaning | Example | Ceiling |
|---|---|---|---|
| `stop_loss_required` | Must be `true`. Every entry needs a stop-loss price. | `true` | always `true` |
| `stop_loss_max_pct` | Most distance of the stop-loss from the limit price, in percent. | `2` | 10 |
| `take_profit_max_pct` | Most distance of an optional take-profit from the limit price, in percent. | `5` | 50 |

The gate rejects an entry whose stop-loss is missing, on the wrong side of
the limit and the mark, too far away, or past the price at which the exchange
would liquidate the position. It also rejects an entry against an open
position on the other side of the same coin (`opposite_position`): the stops
are reduce-only and would protect nothing.

> [!IMPORTANT]
> hl-guard sends the stop-loss, and the take-profit if you named one, with
> the entry as reduce-only market triggers on the exchange, sized to the
> filled lot. An entry is therefore Ioc only. hl-guard shrinks the stop right
> after a partial fill. If the server stops between the fill and the shrink,
> it fixes the stop on its next start or next write call. Hyperliquid does
> not check the side of a stop, so this gate check is what keeps a stop-loss
> from firing at once.

### Prices and freshness

| Key | Meaning | Example | Ceiling |
|---|---|---|---|
| `price_band_pct_max` | Most distance of an entry's limit price from the mark, in percent. | `0.5` | 5 |
| `snapshot_max_age_s` | Most age, in seconds, of the account read and of the marks the gate uses. | `5` | 30 |
| `close_sell_floor` | A close that sells goes no lower than this times the last mark. Default 0.5. | `0.5` | 0.1 to 0.95 |
| `close_buy_cap` | A close that buys goes no higher than this times the last mark. Default 2. | `2` | 1.05 to 10 |

Closes skip the price band, so a close passes even when the mark lags. A
close is a reduce-only Ioc order at the floor or the cap. A tight floor or cap
can refuse a close in a fast market until the mark catches up.

The exchange can refuse a close with the error "Price too far from oracle".
Then the `close_position` answer is `rejected` and says so, nothing was
closed, and the stops stay on the exchange. `halt` keeps HALT set and lists
the coin under `refused` and `unfinished`.

### Limits for actions that no tool sends yet

The gate checks these limits, but no MCP tool in this version sends a margin
addition or a `scheduleCancel`. They are in the policy so that a later version
cannot send those actions without a limit.

| Key | Meaning | Example | Ceiling |
|---|---|---|---|
| `margin_additions_per_day_max` | Most margin additions per UTC day. | `3` | 10 |
| `add_margin_usd_max` | Most USD one margin addition may move. Default 10, or `per_order_usd_max` if that is lower. | `10` | `per_order_usd_max` |
| `schedule_cancel_max_s` | Most seconds ahead for a dead-man `scheduleCancel`. Default 86400. | `86400` | 5 to 604800 |

### Approval

`approval` decides who sends an allowed entry. Closes, cancels and `halt`
never wait.

| Value | What happens |
|---|---|
| `always` | Every allowed entry waits until you run `hl-guard approve PROPOSAL_ID` in a terminal. This is the default in the example. |
| `{above_usd: N}` | An entry waits for you when your total exposure after it would be above N USD. Small entries into a small book go out alone; anything that grows the book past N waits. hl-guard requires N to be below `per_order_usd_max`. |
| `never` | hl-guard sends every allowed entry itself. `hl-guard serve` refuses to start without `--i-understand-autonomous`. |

When an entry waits, the client gets a proposal id. The proposal waits for
`approval_ttl_s` seconds, 120 by default, set in the serve config. Then it
expires and nothing is sent. To answer it:

<!-- docs-test: skip needs-proposal -->
```sh
uv run hl-guard pending --config ~/.config/hl-guard/serve.yaml
uv run hl-guard approve PROPOSAL_ID --config ~/.config/hl-guard/serve.yaml
uv run hl-guard reject PROPOSAL_ID --config ~/.config/hl-guard/serve.yaml
```

`approve` runs only in a terminal and asks you to type `yes`. After it, the
running server checks the entry against the policy again with a fresh account
read before it sends it.

With `{above_usd: N}` or `never`, entries go out with no person in the loop.
Each still carries its stop-loss on the exchange. `hl-guard serve` prints a
warning that says so when it starts.

## The serve config

`hl-guard serve`, `init`, `status`, `revoke`, `approve`, `reject` and
`pending` take `--config`, the serve config. See
[examples/serve.yaml](../examples/serve.yaml) for every key.

| Key | Meaning |
|---|---|
| `policy` | The policy file. A relative path is read from the config's folder. |
| `data_dir` | The folder for the ledger, the `HALT` file and the mode file. Create it with `mkdir -m 700`. It must not be under a temp folder. |
| `account` | The public address of your master account. |
| `agent_key_file` | Optional. The agent key that `hl-guard init` creates. Without it, the server has read tools only. |
| `approval_ttl_s` | Optional. Seconds a proposal waits for approval, 10 to 900. Default 120. |
| `info_weight_per_min` | Optional. The Hyperliquid read budget per minute, 20 to 600. Default 300. |
| `info_timeout_s` | Optional. Seconds one read may take, 1 to 60. Default 10. |
| `info_budget_file` | Optional. Where servers on this machine share the read budget. |

## Halt

The `halt` tool writes a `HALT` file in the data dir, cancels every opening
order and closes every position reduce-only. While the file exists, every
entry is rejected. No tool removes it. To trade again, delete it:

<!-- docs-test: skip needs-halt -->
```sh
rm ~/.hl-guard/HALT
```

## Mainnet

The default network is testnet. To trade on mainnet, you need all three:

1. `network: mainnet` in the policy.
2. A file named `mode` in the data dir that says `mainnet`, that only you
   can write.
3. `--expect-network mainnet` on `hl-guard serve` and on `hl-guard init`,
   `status` and `revoke`.

Without all three, hl-guard refuses to start. Use a separate data dir and a
separate agent key for mainnet. Run the quick start on testnet first, and
start with small limits.
