# Changelog

## 0.2.0 (Unreleased)

### Stops on the exchange

- Every entry carries its stop-loss, and an optional take-profit, on the
  exchange. `propose_order` sends one `normalTpsl` action: the entry and its
  stops as reduce-only market triggers, sized to the filled lot.
- Entries are Ioc only, since the stops go with the entry. `tif` accepts only
  `Ioc`, and `hl-guard approve` refuses a pending entry that is not Ioc.
- hl-guard shrinks the stop right after a partial fill, with a `batchModify`
  by cloid. If the modify fails or its answer is unclear, it cancels the
  stops, closes the filled lot with a reduce-only Ioc order and answers with
  a warning. If the server stops between the fill and the shrink, it fixes
  the stop on its next start or next write call.
- On start and at the start of every write tool, hl-guard checks its open
  stops against the exchange: it learns their oids, records the ones that
  fired or were cancelled, and shrinks a stop larger than its lot. It runs
  before any refusal, also under HALT, since a shrink and a reduce-only close
  only lower risk. When it cannot shrink a stop, the answer of that write
  call, or of the first one after a start, carries a `warning`.
- `close_position` comes out of hl-guard's own lot first. When that lot is
  gone it cancels its stops on the coin; when part is left it shrinks them.
  `halt` cancels the stops of each coin whose close filled and keeps those
  of a coin whose close failed.
- `proposal_status` has a new field `protection`: each stop with its cloid,
  trigger, size and state. `positions` has a new field `stop_warnings` for
  an hl-guard stop larger than hl-guard's own lot.
- Fills of the stops count toward the daily loss and the loss streak.
- The ledger has new `child` and `child_resize` records. A 0.1.0 reader
  refuses a ledger that holds them.

### Gate

- New rule `opposite_position`: an entry against an open position on the
  other side of the coin is rejected, because its reduce-only stops would
  protect nothing.

### Signer

- `batchModify` joins the allowlist, only as the shrink of a stop that the
  ledger records: by cloid, everything but the size unchanged, the size
  smaller. A halt lets this shrink out.
- An `order` with grouping `normalTpsl` is allowed only as an Ioc entry
  followed by one stop-loss and at most one take-profit, each of the entry's
  size, on the exit side, with the stop-loss on the loss side of the entry
  price. `positionTpsl` and every other grouping stay refused.

### Changed

- When the exchange refuses a close with "Price too far from oracle",
  `close_position` answers `rejected` and says so; nothing was closed and
  the stops stay. `halt` keeps HALT set, lists the coin under `refused` and
  `unfinished`, and keeps its stops.

### Known limits

- On testnet, a close of some coins was refused with "Price too far from
  oracle", and the exchange does not document the band.
- A missed Ioc entry is refused, and its stops are never placed
  (testnet-confirmed): the exchange answers one error for the entry and its
  stops. hl-guard checks the open orders once; a stop still listed, or a
  read that fails, sets HALT. Any other answer shape it does not know sets
  HALT too.
- A close of hl-guard's lot in the Hyperliquid app leaves its stops live
  against your remaining coins. `positions` warns only when a stop is larger
  than the position or than hl-guard's own net fills.

### Docs

- [docs/separate-user.md](docs/separate-user.md): run hl-guard, its key and
  its ledger under a separate user on macOS or Linux, so that a client with
  a shell cannot approve its own orders or read the key.
- [docs/faq.md](docs/faq.md): sub-accounts, a crash after a fill, a stolen
  agent key, what is tested, who reviewed it, and what the ledger chain
  catches.
- README: a risk notice and a "Status and review" section.
- [RELEASING.md](RELEASING.md) and a release workflow that publishes to PyPI
  with trusted publishing.

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
