# FAQ

Short answers about what hl-guard 0.2.0 does and does not do. The
[threat model](threat-model.md) has the full list.

## Why not just use a sub-account with a small balance?

Keep a small balance, and use hl-guard as well. They limit different things.

- A small balance caps the total you can lose.
- hl-guard caps each step: the coins, the size per order, the total exposure,
  the leverage, the entries per day, the daily loss and the loss streak. With
  `approval: always`, a person approves every entry.

A small balance does not stop a model from spending it in one order, and
hl-guard does not stop a slow loss within your limits.

hl-guard trades only the account in the `account` line of the config.
It never sets the vault address that Hyperliquid uses to trade a sub-account,
so it cannot trade a sub-account of that account. To cap the total, use a
separate master wallet that holds only what you are willing to lose.

## What happens if the server dies right after a fill?

The stop is on the exchange from the moment of the fill; a partial fill is
shrunk on the next start.

- hl-guard sends the stop-loss, and the take-profit if you named one, with
  the entry in one action. Hyperliquid places them as reduce-only market
  triggers when the entry fills and runs them whether the server runs or
  not.
- Hyperliquid sizes the stops to the ordered size, even when only part of
  the entry fills. hl-guard shrinks the stop right after a partial fill. If
  the server stops between the fill and the shrink, it fixes the stop on its
  next start or next write call. In that window the stop is larger than the
  lot. It is reduce-only, so it cannot open a position, but if it fires it
  can sell coins you hold on the same side.
- If hl-guard cannot shrink the stop, it cancels it, closes the filled lot
  with a reduce-only order and says so in a warning.

## What can a stolen agent key do outside hl-guard?

Outside hl-guard, no policy applies. The key can sign whatever Hyperliquid
accepts from an agent key for your account. That includes orders of any
size, at any leverage the exchange allows, until the approval expires or you
replace the agent. Hyperliquid documents that an agent key cannot withdraw
funds. hl-guard does not test that.

If you lose the key, replace the agent. `hl-guard revoke` creates a new key
and serves a page where your wallet approves it under the name `hl-guard`.
The new approval replaces the old agent on the exchange:

<!-- docs-test: skip wallet -->
```sh
uv run hl-guard revoke --config ~/.config/hl-guard/serve.yaml --days 7
```

If the data dir is gone too, approve any agent named `hl-guard` from your
master wallet, for example on the API page of the Hyperliquid app. That also
replaces the old one.

Keep the approval short. `--days` on `hl-guard init` and `hl-guard revoke`
takes 1 to 180 days, 30 by default. A shorter approval leaves a stolen key
less time.

## How much is tested against the real testnet?

- **The test suite runs offline.** When this page was written,
  `uv run pytest --co -q` counted 2686 tests. They run against fakes of the
  Hyperliquid API and local servers. The suite allows only loopback
  connections, and a test checks that.
- **The testnet run is by hand.** `tools/e2e/run.sh` runs on the testnet: the
  agent check, an entry that the policy rejects, an entry that you approve in
  a second terminal, its fill, a close and a ledger that verifies. It runs
  before each release, see [RELEASING.md](../RELEASING.md). CI does not run
  it.
- **The stops of 0.2.0 were probed by hand.** A probe script sent the exact
  entry and stop shapes to the testnet on 2026-11-19: full and partial Ioc
  fills, the one-cancels-other pair, a stop on the wrong side, a close made
  outside hl-guard and a shrink by batchModify. The lane
  `tools/e2e/run.sh --lane tpsl` runs an entry with its stops through
  hl-guard itself; it has not run on the testnet yet.

## Who reviewed it besides AI?

No independent person yet. The exchange core, the signer and the action
allowlist are ported from a private trading system that has run on
Hyperliquid mainnet since October 2026. The hl-guard server, that is the MCP
tools, the approval and the policy gate, has a large offline test suite and
end-to-end runs on the testnet. It has had no independent human security
review yet. Reports are welcome, see [SECURITY.md](../SECURITY.md).

## What does the ledger hash chain protect against?

Each ledger line holds the hash of the line before it.
`hl-guard ledger verify` checks the chain.

It catches:

- a line that is edited, dropped or moved in the middle of the file. The
  chain breaks from that line on.
- a last line that a crash cut short. `verify` reports it, and
  `hl-guard ledger repair` fixes it.

It does not catch, on its own:

- whole lines dropped from the end. The shorter file is still a valid chain.
  To catch this, keep the record count and the head hash that `verify`
  prints, `verify ok: N records, head …`, somewhere else, and compare them
  later.

Against the model, the chain is not the control. No MCP tool writes an
approval, a latch clear or an unhalt record, and no tool edits or removes a
record. See the [threat model](threat-model.md).

The chain does not protect against a process that runs as your user. Such a
process can write a valid approval, or rewrite the file and compute the
hashes again. To keep the ledger out of its reach, run hl-guard under a
[separate user](separate-user.md).
