# Security policy

hl-guard handles trading keys, so treat any flaw that lets an order bypass the
policy, leaks a key, or moves funds as a security issue.

## Report a vulnerability

Report privately through GitHub private vulnerability reporting. Open the
**Security** tab of this repository and select **Report a vulnerability**.

Do not open a public issue, pull request, or discussion for a security report.

Include the version or commit, the steps to reproduce, and the impact you
expect. We aim to acknowledge a report within 7 days.

## Known limits

`hl-guard approve` keeps the approval with a person only when the client that
proposes orders cannot run commands as your user and cannot write to the
hl-guard data dir or config.

- An MCP client with a shell tool, such as a coding agent with a terminal,
  runs under your user ID. It can start `hl-guard approve` in a
  pseudo-terminal and type `yes`, so it can approve its own proposal. The
  terminal check slows this down but does not stop it.
- The same client can read the agent key file and sign orders without
  hl-guard.
- Any process of your user that can write to the data dir can approve without
  a terminal: an approval is a ledger record, and a write of one with the
  right hash approves a pending proposal. Since a restart places the
  approvals it finds in the ledger, such a process can also add a proposal
  and its approval that the next serve places after a fresh gate pass.
- The same process can lift a halt by deleting the `HALT` file in the data
  dir, and can drop a latch by rewriting the ledger without the latch record
  and chaining the hashes again, so the next serve starts unlatched.
- A process that can write the config or the policy file can change the
  limits the next serve enforces.

To keep approvals and the key out of the client's reach, run the trading
client without shell tools and without write access to the hl-guard data dir,
config and policy file, or run hl-guard and its key under a separate user that
the client cannot reach. [docs/separate-user.md](docs/separate-user.md) shows
how to set that up on macOS and Linux.

## Supported versions

hl-guard is pre-alpha. Only the latest commit on `main` gets fixes.
