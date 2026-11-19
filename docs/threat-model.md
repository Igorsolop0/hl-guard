# Threat model

This page lists who can act on your Hyperliquid account through hl-guard,
what each actor can do, and the control that stops each attack. It describes
the code on `main`. Report a flaw privately, see
[SECURITY.md](../SECURITY.md).

## Assets

1. **Funds on the master account.** Your money on Hyperliquid.
2. **The agent key.** A local private key that Hyperliquid accepts for
   trading on behalf of the master account.
3. **The policy file.** Your limits.
4. **The ledger.** The record of every proposal, decision and exchange
   answer.

## Actors

| Actor | Can reach | Trust |
|---|---|---|
| You | Everything on your machine | Trusted |
| The model behind the MCP client | The MCP tools only | Untrusted. It can be wrong, confused or steered by injected text. |
| Text inside tool results | The model's context | Untrusted. Coin names, order book data, news that a client adds. |
| Another MCP server in the same client | The model's context | Untrusted |
| Malware that runs with your rights | Your files | Out of scope beyond file modes. It owns the machine. |
| The Hyperliquid API | Answers to hl-guard's requests | Trusted for state, checked for shape |

## Controls that hold even if the model is hostile

1. **No withdrawal path.** hl-guard never builds a withdrawal, a transfer,
   a vault or a sub-account action. It builds only 7 action types: `order`,
   `batchModify`, `cancel`, `cancelByCloid`, `scheduleCancel`,
   `updateLeverage` and `updateIsolatedMargin`, and the signer refuses
   anything else. A grouped `order` is only an Ioc entry followed by its
   stop-loss and an optional take-profit, each a reduce-only market trigger
   of the entry's size on the exit side. A `batchModify` only shrinks a stop
   that the ledger records: by its cloid, with the same asset, side, price,
   trigger and type, and a smaller size. A test fails
   the build if a helper for a user-signed action, such as a withdrawal or a
   USD or spot send, appears in the package. The one exception is the
   approve page of `hl-guard init`, which builds the `approveAgent` message
   for your own wallet to sign in your browser. hl-guard never signs a
   user-signed action itself.
2. **The policy is out of reach of MCP.** No tool writes the policy, the key
   or the network mode. No tool writes an approval, a latch clear or an
   unhalt record, and no tool edits or removes a ledger record: a tool call
   only makes the server append its own records, such as the proposal, the
   decision, the order and the exchange answer. The server loads the policy
   once at start. A change needs a restart by you.
3. **Every entry passes the risk gate.** Coins, size per order, total
   exposure, leverage, a required stop-loss on the loss side of the limit and
   a fresh mark, the take-profit cap, no entry against a position on the
   other side, the price band, data freshness, daily loss, loss streak,
   cooldown, entries per day and open orders. The gate runs in the server process after the arguments
   are parsed and before anything is signed. Product hard ceilings bound
   every limit.
4. **Approval happens outside the chat.** With `approval: always`, you
   approve each entry with `hl-guard approve` in a terminal. No MCP tool
   approves, so the model cannot approve its own proposal through MCP. After
   your approval the server runs the whole gate again on a fresh read.
5. **Reducing risk is always allowed.** `close_position` and `halt` are
   reduce-only and skip the size limits and the cooldown. A hostile model can
   therefore close your positions. The cost is fees and spread. Re-opening is
   bounded by the cooldown, the entries per day and the daily-loss latch.
   Closes skip the entry price band. The only price bound on a close is the
   exit cap: a sell no lower than `close_sell_floor` times the last mark and a
   buy no higher than `close_buy_cap` times it. Accepted risk at both ends:
   with a floor of 0.95 a close is refused while the mark lags the book by
   more than 5 %; with a floor of 0.1 a sell on a stale mark can fill far
   below it.
6. **SL (and optional TP) on the exchange for every hl-guard entry, sized to
   the filled lot.** The stops go out with the entry in one action, so the
   stop is on the exchange from the moment of the fill. Hyperliquid does not
   check the side of a stop: on testnet a stop-loss on the wrong side fired
   about one second after the fill. The gate's side and distance check on a
   fresh mark is the guard, and it runs before signing. A hostile model
   cannot widen the stop: the gate caps the distance and the signer pins the
   shape. hl-guard shrinks the stop right after a partial fill. If the server
   stops between the fill and the shrink, it fixes the stop on its next start
   or next write call. A `close_position` comes out of hl-guard's own lot
   first; its stops are cancelled when that lot is gone and shrunk when part
   of it is left. `halt` cancels the stops of each coin whose close filled
   and keeps those of a coin whose close failed.
7. **Pinned endpoints.** hl-guard posts only to the pinned Hyperliquid URL of
   the configured network. Mainnet needs the policy, a mode file in the data
   dir and a start flag to agree.

## What is not protected

- **The crash window.** hl-guard shrinks the stop right after a partial
  fill. If the server stops between the fill and the shrink, it fixes the
  stop on its next start or next write call. Until then the stop is the
  ordered size, larger than the lot. It is reduce-only, so it never opens a
  position, but it can sell coins you hold on the same side.
- **A close in the app.** If you close hl-guard's lot in the Hyperliquid
  app, its stops stay live at their size against your remaining coins.
  Hyperliquid cancels them only when the position reaches zero. `positions`
  warns when an hl-guard stop is larger than hl-guard's own lot, that is its
  own net fills capped by the position. When you hold coins on the same side,
  hl-guard cannot tell your coins from its own, and no warning shows. Cancel
  such stops in the app.
- **You change the stop in the app.** hl-guard records the state it finds:
  a new oid, a cancel or a fill. A stop that is then larger than hl-guard's
  lot is shrunk back to the lot on the next write call.
- **No person in the loop.** With `approval: never` or `{above_usd: N}`,
  entries go out with no person in the loop, and `hl-guard serve` warns about
  it at start.
- **A client with a shell tool.** An MCP client that can run commands as your
  user, such as a coding agent with a terminal, can start `hl-guard approve`
  in a pseudo-terminal and type `yes`, so it can approve its own proposal.
  The terminal check slows this down but does not stop it. The same client
  can read the agent key and sign orders without hl-guard.
- **A process that can write the data dir or the config.** An approval is a
  ledger record, so a process that can write the data dir can approve without
  a terminal. It can also delete the `HALT` file, or rewrite the ledger
  without a latch record and chain the hashes again. A process that can write
  the config or the policy changes the limits of the next start.

To keep approvals and the key out of the client's reach, run the trading
client without shell tools and without write access to the hl-guard data
dir, config and policy, or run hl-guard and its key under a separate user
that the client cannot reach.

## Attacks and answers

| Attack | Outcome with hl-guard |
|---|---|
| Prompt injection asks the model to "send all funds to 0x…" | No tool can express it. No action type exists for it. |
| The model opens the largest allowed size many times | Total exposure, open orders, entries per day and the cooldown cap it. |
| The model churns positions to burn fees | Cooldown, entries per day, the loss streak and the daily-loss latch cap it. Residual: fees up to the daily loss allowance. |
| The model sets the stop-loss on the profit side, so it fires at once | The gate rejects a stop-loss that is not on the loss side of both the limit and a fresh mark. The signer also refuses a grouped entry whose stop-loss trigger is on the profit side of the entry price. |
| The model enters against your open position | `opposite_position` rejects an entry on the other side of a held position, where the reduce-only stops would protect nothing. |
| The model sends a modify to move or grow a stop | The signer builds a `batchModify` only to shrink a stop that the ledger records, with everything but the size unchanged. |
| The model asks for more leverage | An entry above `leverage_max` is rejected. Entries are isolated only. |
| The model claims "the user approved this" | Only `hl-guard approve` in a terminal approves. Words in the chat do not. |
| The model tries to edit the policy through another tool, such as a filesystem MCP server | Keep the policy outside any folder such a tool can write; the docs use `~/.config/hl-guard/`. hl-guard refuses a policy that group or others can write. A change applies only after a restart, which you see. Residual: a tool with write access to that path. |
| A stolen agent key | Through hl-guard the thief can only trade. Used outside hl-guard, the key can sign whatever Hyperliquid accepts from an agent key; hl-guard does not test that list, so revoke a key you lost. An approval made through `hl-guard init` expires after at most 180 days, the key file is mode 600, and `hl-guard revoke` replaces the agent. Residual: trading losses, and whatever else Hyperliquid accepts from an agent key, until you revoke it. |
| Replay of an old approval | An approval names a one-time proposal id and the proposal hash, and a proposal expires after its TTL. A second answer to the same id is refused. |
| A client sends malformed arguments | Arguments are parsed into typed values at the boundary. Unknown fields, NaN, negative sizes and unknown coins are rejected. |
| The exchange answers with an unexpected shape | The answer is parsed at the boundary and the call fails. When the outcome of a sent order is unknown, hl-guard writes the `HALT` file, so no new entry goes out until you check the account. |
| You write an unsafe policy | A value outside a product hard ceiling fails `policy check` and the server start, with the ceiling named. |

## Out of scope

- A compromised operating system or user account.
- Market risk. hl-guard limits how much an order can risk, not whether a
  strategy is good.
- Hosted deployments. hl-guard is self-hosted only.

## Risk settings to know

- Leverage goes up to 5x, isolated only, default 1x. At 5x on a coin whose
  exchange maximum is also 5x, liquidation sits about 10 % away, the same
  distance as the widest allowed stop-loss. Slippage can then reach
  liquidation and lose the isolated margin of that position. The gate
  rejects a stop-loss at or past the liquidation distance. The loss stays
  bounded by the size per order.
- Margin additions and `scheduleCancel` windows have limits in the policy,
  but no tool in this version sends them.
