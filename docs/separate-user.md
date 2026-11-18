# Run hl-guard under a separate user

Claude Code, Cursor and other clients with a shell tool run commands as your
user. Such a client can run `hl-guard approve` itself and read the agent key,
so on its own the approval step does not hold against it. See
[SECURITY.md](../SECURITY.md#known-limits).

This guide moves hl-guard, its key, its ledger, its config and its policy to a
separate operating-system user, here called `hlguard`. Your user, and so the
client, can then start exactly one command as `hlguard`: `hl-guard serve` with
your config. It cannot run `hl-guard approve`, cannot read the key and cannot
write the ledger, the policy or the code. You approve entries in your own
terminal with your password.

Every step needs root, so the docs tests do not run them. Read each command
before you run it.

## What you get, and what you do not

The separate user holds only if all of these are true:

- **The client cannot become root or `hlguard` without your password.** If
  your user can run `sudo` without a password, or a cached `sudo` login is
  still valid in the terminal of the client, the client can run anything as
  `hlguard`. The sudoers file below makes `sudo` ask for the password every
  time.
- **The client cannot write the code that `hlguard` runs.** If it could, it
  would change hl-guard and the next start would run that change as
  `hlguard`. This guide installs the code and its Python as root, under
  `/opt`.
- **Only `hlguard` can read and write its home folder.** hl-guard refuses to
  start if the config, the policy, the data dir or the agent key belongs to
  another user, or if group or others can write it. The key must have mode
  600 and its folder mode 700.

The client can still call every MCP tool, as before. The policy and the risk
gate apply to those calls as usual.

## 1. Create the user

The commands use these paths:

| Path | What it holds |
|---|---|
| `/var/hlguard` (macOS), `/var/lib/hlguard` (Linux) | Home of `hlguard`: config, policy, data dir, agent key, ledger, lock |
| `/opt/hl-guard` | The hl-guard code and its virtual environment, owned by root |
| `/opt/hl-guard-python` | The Python that uv installs for it, owned by root |

The data dir must not be under a temp folder, such as `/tmp`, `/var/tmp` or
`/var/folders`. hl-guard refuses one there.

### macOS

Pick a user ID and a group ID that are free. This lists the ones in use:

```sh
dscl . -list /Users UniqueID
dscl . -list /Groups PrimaryGroupID
```

The commands below use 451. If it is taken, use another number below 500 in
every line. Create a hidden user that cannot log in, with its own group and
home:

<!-- docs-test: skip root -->
```sh
HLG_USER=hlguard
sudo dscl . -create "/Groups/$HLG_USER"
sudo dscl . -create "/Groups/$HLG_USER" PrimaryGroupID 451
sudo dscl . -create "/Users/$HLG_USER"
sudo dscl . -create "/Users/$HLG_USER" UniqueID 451
sudo dscl . -create "/Users/$HLG_USER" PrimaryGroupID 451
sudo dscl . -create "/Users/$HLG_USER" UserShell /usr/bin/false
sudo dscl . -create "/Users/$HLG_USER" NFSHomeDirectory /var/hlguard
sudo dscl . -create "/Users/$HLG_USER" Password '*'
sudo dscl . -create "/Users/$HLG_USER" IsHidden 1
sudo mkdir -m 700 /var/hlguard
sudo chown hlguard:hlguard /var/hlguard
```

A user that you create in **System Settings**, then **Users & Groups** works
too, but it is a full login user that shows on the login screen. Use its
home folder in place of `/var/hlguard` in every step.

### Linux

<!-- docs-test: skip root -->
```sh
sudo useradd --system --create-home --home-dir /var/lib/hlguard --shell /usr/sbin/nologin hlguard
sudo chmod 700 /var/lib/hlguard
```

`--create-home` matters: hl-guard keeps its lock and its request budget under
the home of the user that runs it, and refuses to start without one.

## 2. Install the code as root

Install hl-guard and its Python under `/opt`, owned by root, so that neither
your user nor `hlguard` can change them:

<!-- docs-test: skip root -->
```sh
sudo git clone https://github.com/Igorsolop0/hl-guard.git /opt/hl-guard
cd /opt/hl-guard
sudo -H env UV_PYTHON_INSTALL_DIR=/opt/hl-guard-python UV_PYTHON_PREFERENCE=only-managed "$(command -v uv)" sync --frozen --no-dev
sudo -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard --version
```

The last command prints the version. It runs as `hlguard`, so it also shows
that `hlguard` can run the code.

- `-H` gives root its own home, so uv does not write root-owned files into
  your cache.
- `UV_PYTHON_PREFERENCE=only-managed` makes uv install its own Python under
  `/opt/hl-guard-python`. Without it, uv may pick a Python that your user can
  write, such as one from Homebrew.

To update later, run `sudo git pull` in `/opt/hl-guard`, then the same
`uv sync` line, and restart the client.

## 3. Write the config and the policy

On Linux, use `/var/lib/hlguard` in place of `/var/hlguard` in this and every
later step, starting with `HLG_HOME` below. Put your master account address
in a variable, as in the
[quick start](quickstart.md#2-write-the-policy-and-the-config):

<!-- docs-test: skip your-value -->
```sh
HL_ACCOUNT=0xYourMasterAccountAddress
```

Create the folders as `hlguard`, copy the example policy, and install a
config with absolute paths:

<!-- docs-test: skip root -->
```sh
HLG_HOME=/var/hlguard
sudo -H -u hlguard mkdir -p -m 700 "$HLG_HOME/.config/hl-guard" "$HLG_HOME/.hl-guard"
sudo install -o hlguard -g hlguard -m 600 /opt/hl-guard/examples/policy.yaml "$HLG_HOME/.config/hl-guard/policy.yaml"
cat > "$HOME/hlguard-serve.yaml" <<EOF
policy: $HLG_HOME/.config/hl-guard/policy.yaml
data_dir: $HLG_HOME/.hl-guard
account: "$HL_ACCOUNT"
agent_key_file: $HLG_HOME/.hl-guard/agent.key
info_budget_file: $HLG_HOME/.hl-guard/info-weight.json
approval_ttl_s: 300
EOF
sudo install -o hlguard -g hlguard -m 600 "$HOME/hlguard-serve.yaml" "$HLG_HOME/.config/hl-guard/serve.yaml"
rm "$HOME/hlguard-serve.yaml"
sudo -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard policy check "$HLG_HOME/.config/hl-guard/policy.yaml"
```

The last command prints `policy /var/hlguard/.config/hl-guard/policy.yaml ok`
and one line per limit. To change the policy later, edit it as `hlguard`, for
example with `sudo -u hlguard vi /var/hlguard/.config/hl-guard/policy.yaml`,
and restart the client.

## 4. Approve the agent key as hlguard

Run `hl-guard init` as `hlguard` in your own terminal. It writes the key into
the data dir of `hlguard`, prints a link to a page on `127.0.0.1`, and waits.
Open the link in your browser and sign with your wallet, as in
[step 3 of the quick start](quickstart.md#3-approve-the-agent-key):

<!-- docs-test: skip wallet -->
```sh
sudo -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard init --config /var/hlguard/.config/hl-guard/serve.yaml --expect-network testnet
```

`hl-guard status` and `hl-guard revoke` work the same way, with the same
`sudo -H -u hlguard` prefix.

## 5. Let your user start the server, and nothing else

Find your login name with `whoami`. Open a new sudoers file with `visudo`,
which refuses a file with a syntax error:

<!-- docs-test: skip root -->
```sh
sudo visudo -f /etc/sudoers.d/hl-guard
```

Write these lines into it. Replace `alice` with your login name, and on
Linux the home path:

```text
Defaults:alice timestamp_timeout=0
Defaults>hlguard always_set_home
alice ALL=(hlguard) NOPASSWD: /opt/hl-guard/.venv/bin/hl-guard serve --config /var/hlguard/.config/hl-guard/serve.yaml --expect-network testnet
```

- The last line lets your user start this one command as `hlguard` without
  a password. sudo compares the whole command line, so the same program
  with other arguments, such as `approve`, still needs a password.
- The first line makes every other `sudo` of your user ask for the password,
  so a client cannot use a `sudo` login that you typed a minute ago. This
  applies to all your `sudo` commands, not only to hl-guard.
- The second line sets `HOME` to the home of `hlguard` whenever a command
  runs as `hlguard`, even without `-H`. hl-guard keeps its one-server lock
  under `HOME`, so a client must not choose it. macOS keeps the caller's
  `HOME` by default.

Both macOS and common Linux distributions read `/etc/sudoers.d`. Check what
your user may run:

<!-- docs-test: skip root -->
```sh
sudo -l
```

## 6. Point the client at sudo

The client starts the server through `sudo`. `-n` makes sudo fail instead of
asking for a password, so a wrong line shows up as an error in the client:

```json
{
  "mcpServers": {
    "hl-guard": {
      "command": "/usr/bin/sudo",
      "args": ["-n", "-H", "-u", "hlguard", "/opt/hl-guard/.venv/bin/hl-guard", "serve",
               "--config", "/var/hlguard/.config/hl-guard/serve.yaml",
               "--expect-network", "testnet"]
    }
  }
}
```

The arguments after `hlguard` must match the sudoers line exactly. Use the
block with your client as in [clients.md](clients.md). Remove any older
hl-guard entry that runs as your user.

## 7. Approve in your own terminal

When an entry waits, list it and approve it in a terminal that the client
does not use. sudo asks for your password:

<!-- docs-test: skip needs-proposal -->
```sh
sudo -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard pending --config /var/hlguard/.config/hl-guard/serve.yaml
sudo -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard approve PROPOSAL_ID --config /var/hlguard/.config/hl-guard/serve.yaml
```

This needs a user that may run any command as `hlguard` with a password. An
admin user on macOS and a member of the `sudo` or `wheel` group on most Linux
systems may. Otherwise add this line to the same sudoers file, above the
`NOPASSWD` line:

```text
alice ALL=(hlguard) /opt/hl-guard/.venv/bin/hl-guard
```

The order matters: when several lines match a command, sudo uses the last
one. Below the `NOPASSWD` line, this line would also match `serve`, and the
client could no longer start the server without a password.

Check the ledger the same way:

<!-- docs-test: skip needs-ledger -->
```sh
sudo -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard ledger verify /var/hlguard/.hl-guard/ledger.jsonl
```

## 8. Check that the client is locked out

Run these as your own user, the user the client runs as:

<!-- docs-test: skip root -->
```sh
cat /var/hlguard/.hl-guard/agent.key
sudo -k
sudo -n -H -u hlguard /opt/hl-guard/.venv/bin/hl-guard pending --config /var/hlguard/.config/hl-guard/serve.yaml
```

The first command fails with `Permission denied`. The last one fails with
`sudo: a password is required`. If either prints anything else, the client
can reach the key or the approval, and the setup does not protect you.

## Mainnet

For mainnet, follow [policy.md](policy.md#mainnet) as `hlguard`: the mode file
goes into `/var/hlguard/.hl-guard`, owned by `hlguard`. Change
`--expect-network testnet` to `--expect-network mainnet` in the sudoers line
and in the client block, in the same way.
