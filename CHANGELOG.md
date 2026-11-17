# Changelog

## 0.1.0

The first release. hl-guard is pre-alpha: test it on the Hyperliquid testnet
before you point it at real funds, and expect breaking changes.

### What it does

- An MCP server over stdio with 8 read tools: `markets`, `order_book`,
  `candles`, `account`, `positions`, `open_orders`, `fills` and
  `risk_status`.
- With an agent key, 5 guarded write tools: `propose_order`,
  `close_position`, `cancel_order`, `halt` and `proposal_status`.
- A risk gate on every entry: coins, size per order, total exposure,
  leverage (isolated only, up to 5x), open orders, entries per day, cooldown
  per coin, daily loss, loss streak, a required stop-loss, a take-profit cap,
  a price band around the mark and a fresh account read. Product hard
  ceilings bound every limit, and a policy outside them fails to load.
- Out-of-band approval. With `approval: always`, an entry waits until you
  run `hl-guard approve` in a terminal and type `yes`. No tool approves.
  After the approval the server runs the whole gate again on a fresh read.
- `close_position` and `halt` only reduce risk and need no approval. A close
  is a reduce-only order at the policy's exit price cap.
- A hash-chained, append-only ledger of every proposal, decision, order and
  exchange answer, and of the fills of orders hl-guard placed.
  `hl-guard ledger verify` checks the chain.
- `hl-guard init` creates a local agent key and serves a local page where
  your browser wallet approves it for 1 to 180 days. `hl-guard revoke`
  replaces it.
- hl-guard signs only 6 action types with the agent key: `order`, `cancel`,
  `cancelByCloid`, `scheduleCancel`, `updateLeverage` and
  `updateIsolatedMargin`. hl-guard never builds a withdrawal, a transfer, a
  vault or a sub-account action.
- Testnet by default. Mainnet needs the policy, a mode file in the data dir
  and a start flag to agree.

### What it does not do

- **It does not place stop-loss or take-profit orders.** The gate requires a
  stop-loss price on every entry and records it, but you or the client must
  place the stop on the exchange.
- It does not protect you from a client that can run shell commands as your
  user, or from any process that can write the data dir, the config or the
  policy. See [SECURITY.md](SECURITY.md).
- It does not count fills of orders you place outside hl-guard, such as a
  stop in the Hyperliquid app, toward the loss streak.
- It does not trade spot or open cross-margin positions.
- It does not run as a hosted service.
- It makes no claim about returns.
