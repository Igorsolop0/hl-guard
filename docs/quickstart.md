# Quick start on testnet

This guide takes about 5 minutes. At the end you have:

- an agent key that your browser wallet approved,
- hl-guard connected to an MCP client, such as Claude Desktop or Claude Code,
- one order that the policy rejected,
- one order that you approved, that filled and that you then closed,
- a ledger that verifies.

Everything here runs on the Hyperliquid **testnet**. No real funds are at risk.

> [!IMPORTANT]
> hl-guard requires a stop-loss price on every entry, but it does **not**
> place stop-loss or take-profit orders on the exchange. It checks the price
> and records it. In this guide you close the position right after it fills.
> If you keep a position open, place its stop yourself in the Hyperliquid app.

## Before you start

You need:

- macOS or Linux, with `git` and [uv](https://docs.astral.sh/uv/). Step 1
  shows how to install them.
- A browser with a wallet extension, such as MetaMask or Rabby. The wallet
  holds your Hyperliquid **master** account, the account that holds the funds.
- Testnet USDC in the perps balance of that account. See the
  [Hyperliquid testnet faucet](https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/testnet-faucet).
  The faucet may require that the address has used Hyperliquid mainnet
  before. If the faucet refuses, ask someone who has testnet USDC to send
  some to your master address on testnet. Without a balance you can still
  try the read tools: do steps 1 and 2, remove the `agent_key_file` line from
  the serve config as shown at the end of step 2, skip step 3, and do steps 4
  and 5. Entries need the balance; without it they are rejected with
  `no_equity`.
- The account set to the **Manual** account type, with the USDC in **Perps**.
  New Hyperliquid accounts start as a Unified Account. There the perps
  balance that hl-guard reads is 0 while the USDC sits in Spot, so every
  entry is rejected with `no_equity`. In the Hyperliquid testnet app, open
  **Portfolio**, then **Account Type**, choose **Manual**, and transfer the
  USDC from Spot to Perps. A Send from another account also lands in Spot,
  so transfer it the same way.
- An MCP client. This guide shows Claude Desktop, which runs on macOS and
  Windows but not Linux. **On Linux, or with Claude Code or Cursor,** do
  steps 1 to 4 here, then follow [clients.md](clients.md) instead of step 5.
  Claude Code works on both systems.

## 1. Install

Check whether `git` and `uv` are installed:

<!-- docs-test: skip network -->
```sh
git --version
uv --version
```

If a command is not found, install it:

- `git`: on macOS run `xcode-select --install`. On Linux use your package
  manager, for example `sudo apt install git`.
- `uv`: run the installer from the uv docs, then open a new terminal.

<!-- docs-test: skip network -->
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then get hl-guard:

<!-- docs-test: skip network -->
```sh
git clone https://github.com/Igorsolop0/hl-guard.git
cd hl-guard
uv sync --frozen
```

Run every command in the rest of this guide from this `hl-guard` folder.
Check that the command works:

```sh
uv run hl-guard --version
```

It prints `hl-guard 0.1.0`.

## 2. Write the policy and the config

Find your master account address: open the wallet extension and copy the
address of the account that holds your Hyperliquid funds. It is `0x` and 40
hex digits, the same address the Hyperliquid app shows when this wallet is
connected. Put it in a shell variable. Only the next two blocks use it, so
run them in the same terminal:

<!-- docs-test: skip your-value -->
```sh
HL_ACCOUNT=0xYourMasterAccountAddress
```

Create the folders, copy the example policy, and write the serve config. The
config folder and the data folder must be private to you, so they get mode
700. The files get mode 600.

```sh
mkdir -p ~/.config/hl-guard
chmod 700 ~/.config/hl-guard
mkdir -p -m 700 ~/.hl-guard
cp examples/policy.yaml ~/.config/hl-guard/policy.yaml
chmod 600 ~/.config/hl-guard/policy.yaml
cat > ~/.config/hl-guard/serve.yaml <<EOF
policy: policy.yaml
data_dir: ~/.hl-guard
account: "$HL_ACCOUNT"
agent_key_file: ~/.hl-guard/agent.key
approval_ttl_s: 300
EOF
chmod 600 ~/.config/hl-guard/serve.yaml
grep account ~/.config/hl-guard/serve.yaml
```

The last command prints `account: "0x…"` with your address. If it prints
`account: ""`, the variable was not set in this terminal: set it and run the
block again.

Only without a testnet balance: remove the agent key line, so that the server
starts with the read tools only.

<!-- docs-test: skip no-balance -->
```sh
sed -i.bak '/^agent_key_file:/d' ~/.config/hl-guard/serve.yaml
rm ~/.config/hl-guard/serve.yaml.bak
```

Check the policy:

```sh
uv run hl-guard policy check ~/.config/hl-guard/policy.yaml
```

The first line is `policy /…/policy.yaml ok`. Each line after it shows one
limit, its value and a ceiling or a note. The example policy allows BTC, ETH and SOL,
at most 20 USD per order, 100 USD in total and 1x leverage, and every entry
waits for your approval. `approval_ttl_s: 300` in the config gives you 5
minutes to approve an entry. [policy.md](policy.md) explains each limit.

## 3. Approve the agent key

hl-guard trades with its own agent key, not with your wallet key. Create the
key and approve it with your wallet:

<!-- docs-test: skip wallet -->
```sh
uv run hl-guard init --config ~/.config/hl-guard/serve.yaml
```

The command prints a link to a page on `127.0.0.1` and waits up to 15 minutes:

```text
open this link in the browser that holds the master wallet 0x…:
http://127.0.0.1:…/approve?t=…
agent 0x… on testnet, expires 2026-12-15 12:00 UTC (30 days); waiting up to 900 s, Ctrl-C cancels
```

1. Open the link in the browser that holds your wallet. If the browser has
   more than one wallet extension, the page lists them after you select
   **Sign with wallet**. Choose the one with the master account.
2. Check that the page shows the same agent address as the terminal, and
   **Hyperliquid Testnet**.
3. Select **Sign with wallet** and sign in the wallet.

The page sends the signed approval to the testnet. The terminal then prints:

```text
agent approved, expires 2026-12-15 12:00 UTC
agent 0x… on testnet
```

### If the page does nothing

If the button turns grey and the wallet shows no request, the page tells you
to open the wallet from its toolbar icon. In MetaMask, close the side panel
and open the extension from the toolbar. After 20 seconds without an answer
the button works again.

If the wallet still does not answer, approve the same agent on the
Hyperliquid site instead:

1. Run `hl-guard init` as above, write down the agent address it prints, and
   press Ctrl-C. The key stays.
2. Open the API page of the Hyperliquid testnet app,
   `app.hyperliquid-testnet.xyz/API`, with your master wallet connected.
   Authorize an API wallet with the name `hl-guard` and exactly the agent
   address you wrote down. Do **not** press Generate, which makes a new key
   that hl-guard does not hold. Set Valid until to 180 days or less; hl-guard
   refuses a longer approval.
3. Run `hl-guard init` again. It finds the approval on the exchange and
   records it without a page:

```text
agent approved, expires …
agent 0x… on testnet
```

Before step 3, `hl-guard status` says `the exchange lists the agent … but
hl-guard has not recorded its approval; run hl-guard init`.

You can check the agent at any time:

<!-- docs-test: skip network -->
```sh
uv run hl-guard status --config ~/.config/hl-guard/serve.yaml
```

The key is in `~/.hl-guard/agent.key`. It can place and cancel orders for
your account until it expires. hl-guard never uses it to withdraw or
transfer funds.

## 4. Check that the server starts

Only one `hl-guard serve` with write tools can run per account. A second one
refuses to start with `another hl-guard serve with write tools holds …; stop
it first`. So run this check before you connect a client, stop it before you
go on, and later connect only one client at a time.

<!-- docs-test: skip network -->
```sh
uv run hl-guard serve --config ~/.config/hl-guard/serve.yaml --expect-network testnet
```

The server prints one line to the terminal and then waits for a client:

```text
hl-guard serve: testnet, account 0x…, 8 read tools, 5 write tools, approval always
```

Press Ctrl-C to stop it. From now on the client starts the server.

If the line says `no agent key: read tools only`, the config has no
`agent_key_file` line. If the command prints `hl-guard serve refused:`, the
rest of the line names the problem and how to fix it.

## 5. Connect a client (Claude Desktop shown)

For Claude Code or Cursor, follow [clients.md](clients.md) and then go on
with step 6.

Claude Desktop does not read your shell's `PATH` or expand `~`, so its config
needs absolute paths. This command prints the config block with the paths of
your machine filled in:

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

1. In Claude Desktop, open **Settings**, then **Developer**, then
   **Edit Config**. This opens `claude_desktop_config.json`.
2. Paste the block. If the file already has an `mcpServers` object, add only
   the `hl-guard` entry to it.
3. Quit Claude Desktop completely and open it again.
4. Open the tools menu in a new chat. It lists the hl-guard tools.

To use Claude Code or Cursor instead, follow [clients.md](clients.md).

## 6. Watch the policy reject an order

Ask Claude:

> Use hl-guard to buy BTC worth about 60 USD with a limit price 0.2% above
> the mark, time in force Ioc, and a stop-loss 1% below the limit.

Ioc, "immediate or cancel", means that the order fills at once at the
limit price or better, and the exchange cancels whatever does not fill. No
order is left resting on the book.

Claude calls `propose_order`. The answer is a rejection, because the policy
allows at most 20 USD per order:

```text
{"id":"…","rule":"per_order_usd_max","detail":"order notional 60.… USD is above 20 USD","result":"rejected"}
```

Nothing reached the exchange. If the model sends a price or a size that the
exchange does not accept, the rule says `price_tick` or `size_decimals`
instead. Ask again with a rounder price.

## 7. Approve an order

Ask Claude:

> Now buy BTC worth about 12 USD the same way.

The answer is `pending_approval` with a 12-character proposal id:

```text
{"id":"3f9c1a7b2e40","ttl_s":300,"expires_ms":…,"order":{"coin":"BTC","side":"buy","size":"0.00012",…},"approve_with":"the user approves it in a terminal with: hl-guard approve 3f9c1a7b2e40","result":"pending_approval"}
```

The order waits for you for 300 seconds, the `approval_ttl_s` of your
config. Claude cannot approve it. In a second terminal, in the hl-guard
folder, list and approve it:

<!-- docs-test: skip needs-proposal -->
```sh
uv run hl-guard pending --config ~/.config/hl-guard/serve.yaml
uv run hl-guard approve PROPOSAL_ID --config ~/.config/hl-guard/serve.yaml
```

`pending` prints one line per waiting proposal, or
`no proposal waits for approval`:

```text
3f9c1a7b2e40: buy 0.00012 BTC limit … Ioc at 1x isolated, stop-loss … (recorded and checked by the gate, NOT placed: hl-guard places no stop orders, so you or the client must place them), 287 s left
```

Replace `PROPOSAL_ID` with the 12-character id. Keep Claude Desktop open: only
the server it runs places the order. With no server running, `approve`
refuses with `no hl-guard serve with write tools holds …; start it first`.

`hl-guard approve` shows the order, including the stop-loss, and asks you to
type `yes`:

```text
PROPOSAL_ID: buy 0.00012 BTC limit … Ioc at 1x isolated, stop-loss … (recorded and checked by the gate, NOT placed: hl-guard places no stop orders, so you or the client must place them), … s left
account 0x… on testnet
hash …
Type yes to approve this order:
```

After `yes`, the running server checks the order against the policy again
with a fresh account read, sets BTC to isolated 1x if needed, and sends it.
Ask Claude:

> What is the status of that proposal, and what are my positions?

`proposal_status` says `placed`, and `positions` shows the BTC position.

If the price moved, the exchange can refuse the Ioc order because no seller
is at its limit. `proposal_status` then says `failed`, and no position opens.
A refused order does not count as an entry. Propose it again with a limit up
to 0.4% above the mark, since the policy's band is 0.5%.

The position has **no stop-loss on the exchange**. hl-guard recorded the stop
price but did not place it.

## 8. Close the position

Ask Claude:

> Close my BTC position.

Claude calls `close_position`. A close needs no approval. hl-guard sends a
reduce-only Ioc order, and the answer says `placed` with status `filled`.

## 9. Read the ledger

Every step above is in the ledger. Check the hash chain:

<!-- docs-test: skip needs-ledger -->
```sh
uv run hl-guard ledger verify ~/.hl-guard/ledger.jsonl
```

It prints `verify ok: N records, head …`. Write down the count and the head
hash. A later `verify` that prints a smaller count means that someone dropped
lines from the end.

Show the last records:

<!-- docs-test: skip needs-ledger -->
```sh
uv run hl-guard ledger show ~/.hl-guard/ledger.jsonl --last 30
```

Each line is one JSON record, and its `kind` says what it records. The 60 USD
order has a `proposal` and a `decision` that names the rule. The 12 USD order
has a `proposal`, `decision` records as it moves from pending to placed, your
`approval`, the `order` that hl-guard signed, the exchange's `ack` and a
`fill`. The close has the same records without an approval. The stop-loss
price is in the `proposal` record's `order` object.

## Run it again

The example policy has a cooldown of 3600 seconds per coin and allows 2
entries per UTC day. Within the hour, a second BTC entry is rejected with
`cooldown_s`, so use ETH or SOL, or wait. After 2 entries in a day, every
entry is rejected with `entries_per_day_max` until the next UTC day.

## If something goes wrong

- **Stop everything.** Ask Claude to call `halt`. It writes a `HALT` file in
  `~/.hl-guard`, cancels every opening order and closes every position.
  hl-guard refuses new entries until you delete that file yourself:
  `rm ~/.hl-guard/HALT`.
- **The approval expired.** The order was not sent. Propose it again.
- **`no_equity`.** The perps balance that hl-guard reads is 0. Either the
  account has no testnet USDC, or it is a Unified Account and the USDC is in
  Spot. Switch the account type to Manual under **Portfolio**, then
  **Account Type**, and transfer the USDC from Spot to Perps.
- **The approve page does nothing.** See
  [If the page does nothing](#if-the-page-does-nothing) in step 3.
- **The agent expired or was replaced.** Run `hl-guard status`. It names the
  command that fixes it, `hl-guard init` or `hl-guard revoke`.

## Next steps

- [Write your own policy](policy.md).
- [Read the threat model](threat-model.md) before you use a client that can
  run shell commands.
