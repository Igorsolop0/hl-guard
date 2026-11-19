# hl-guard

hl-guard is a self-hosted MCP server for Hyperliquid perpetuals. An MCP client,
such as Claude Desktop, Claude Code or Cursor, can read markets and propose
orders through it. The server, not the model, decides whether an order goes
out. It checks every proposal against a policy file that you write and that no
tool can change, and it records every step in a hash-chained ledger.

hl-guard is a safety layer. It limits how much a wrong, confused or steered
model can do with your account. It does not make a strategy profitable, and
this project makes no claim about returns.

> [!CAUTION]
> Not financial advice. hl-guard is pre-alpha software that can lose money
> through bugs, wrong settings or market moves. Use it at your own risk, and
> run it on the Hyperliquid testnet before you give it real funds.

## Stops on the exchange

> [!IMPORTANT]
> **Every entry carries its stop-loss on the exchange.** Each entry must name
> a stop-loss price and may name a take-profit price. hl-guard sends both
> with the entry, in one action, as reduce-only market triggers on
> Hyperliquid, so an entry is Ioc only: it fills at once or not at all. The
> stop is on the exchange from the moment of the fill, sized to the filled
> lot. hl-guard shrinks the stop right after a partial fill. If the server
> stops between the fill and the shrink, it fixes the stop on its next start
> or next write call. Until then the stop can be larger than the lot.
>
> A close of hl-guard's lot in the Hyperliquid app leaves its stops on the
> exchange, and they can then sell coins you hold on the same side. Cancel
> them in the app. hl-guard cannot always tell your coins from its own:
> `positions` warns only when a stop is larger than the position, or larger
> than what hl-guard's own fills left open.

## What hl-guard does not do

hl-guard also does not:

- move funds. It has no withdraw or transfer tool, and it never builds a
  withdrawal, transfer, vault or sub-account action for the agent key to sign.
- trade spot, open cross-margin positions, or go above 5x leverage.
- open an entry against a position on the other side of the same coin. The
  gate refuses it, because the reduce-only stops would protect nothing.
- take an approval from the chat. You approve in a terminal with
  `hl-guard approve`.
- protect you from a client that can run shell commands as your user. Such a
  client can run `hl-guard approve` itself and read the agent key. See
  [SECURITY.md](SECURITY.md), and
  [run hl-guard under a separate user](docs/separate-user.md) to close that
  gap.
- run as a hosted service. It runs on your machine only.

## What it does

- **Read tools.** `markets`, `order_book`, `candles`, `account`, `positions`,
  `open_orders`, `fills` and `risk_status`.
- **Guarded write tools**, only after you set up an agent key:
  `propose_order`, `close_position`, `cancel_order`, `halt` and
  `proposal_status`.
- **A risk gate.** Every entry passes the limits in your policy: coins, size
  per order, total exposure, leverage, open orders, entries per day, cooldown
  per coin, daily loss, loss streak, a required stop-loss on the loss side of
  the limit and the mark, no entry against an opposite position, a price band
  around the mark and a fresh account read. Product hard ceilings bound every
  limit. See [docs/policy.md](docs/policy.md).
- **Out-of-band approval.** With `approval: always`, as in the example
  policy, every entry waits until you run
  `hl-guard approve PROPOSAL_ID` in a terminal and type `yes`. No tool approves, and
  words in the chat are not an approval.
- **Closes and halt never wait.** `close_position` and `halt` only reduce
  risk, so they need no approval.
- **A ledger.** Every proposal, decision, order and exchange answer, and
  every fill of an order hl-guard placed, goes on a line in an append-only
  file with a SHA-256 hash chain.
  `hl-guard ledger verify` checks the chain.
- **An agent key that you approve in your browser.** `hl-guard init` creates a
  local trading key and serves a local page where your browser wallet
  approves it. The approval expires after 1 to 180 days, 30 by default.
- **Testnet first.** The default network is the Hyperliquid testnet. Mainnet
  needs a mode file and a start flag. See [docs/policy.md](docs/policy.md).

## Status and review

Pre-alpha, version 0.1.0. Test it on the Hyperliquid testnet before you point
it at real funds. Expect breaking changes.

- The exchange core, the signer and the action allowlist are ported from a
  private trading system that has run on Hyperliquid mainnet since October
  2026.
- The hl-guard server, that is the MCP tools, the approval and the policy
  gate, has a large offline test suite and end-to-end runs on the testnet.
  It has had no independent human security review yet. Reports are welcome,
  see [SECURITY.md](SECURITY.md).

The [FAQ](docs/faq.md) answers common questions about limits, keys and
testing.

## Get started

- [Quick start](docs/quickstart.md): a testnet setup, one rejected order, one
  approved order, a close and the ledger.
- [Connect a client](docs/clients.md): Claude Desktop, Claude Code and Cursor.
- [Write a policy](docs/policy.md): every limit and its ceiling.
- [Threat model](docs/threat-model.md): who can do what, and what stops them.
- [FAQ](docs/faq.md): sub-accounts, a crash after a fill, a stolen key,
  testing and review, the ledger.
- [Separate user](docs/separate-user.md): keep the key and the approval out of
  reach of a client with a shell.

## Develop

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
uv run hl-guard --version
uv run pytest -q
uv run pre-commit install
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full set of checks.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
