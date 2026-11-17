# Contributing

Thank you for your interest in hl-guard.

## Set up

1. Install [uv](https://docs.astral.sh/uv/).
2. Run `uv sync --frozen`.
3. Run `uv run pre-commit install` to enable the gitleaks hook.

## Check your change

Run these before you open a pull request. CI runs the same checks on
Python 3.11 and 3.12.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv run python tools/check_private_traces.py
```

### Private traces check

`tools/check_private_traces.py` catches accidental leaks of personal data. It
checks the contents and the name of every tracked or untracked file that git
does not ignore. The pre-commit hook checks the staged files. The tool reads
UTF-8, Latin-1, UTF-16, UTF-32 and gzip files, and the target of each symlink.

The tool prints only `file:line: rule`, never the matched text. A hit in a file
name has line 0. In that case the tool prints the whole path as `*` characters
on every hit for that file, so the output does not repeat the leak.

CI runs the built-in rules:

- Absolute home paths such as `/Users/<name>`, `/home/<name>` and
  `C:\Users\<name>`. The name can hold letters from any script, digits, `.`,
  `_` and `-`. A directory named `users` or `home` inside the repository also
  matches, so give it another name.
- EVM addresses, `0x` and 40 hex digits. Public constants such as the zero
  address go in `tools/allowed_addresses.txt`.
- Telegram ids. An id is 6 to 12 digits, or a minus sign and 6 to 13 digits.
  The rule matches an id in two places:
  - After a key that contains `chat`, `telegram` or `operator` and ends in
    `id`, `uid`, `user` or `channel`, or in the plural of one of these, with
    at most 40 characters between the two words. Examples are `chat_id`,
    `chatId`, `telegram_user`, `telegram_admin_id`, `TELEGRAM_ALLOWED_USERS`,
    `TELEGRAM_HOME_CHANNEL` and `operator_uid`. In a comma list after the key,
    the first id matches.
  - As the `id` of a `chat`, `from`, `user` or `sender_chat` object in Bot API
    JSON or a Python dict, on one line or across lines. The hit has the line
    number of the id.

  Message, update and completion ids are not personal data. So
  `message_id`, `telegram_message_id`, `msg_id`, `update_id` and
  `chat_completion_id` do not match. A bare `uid` or `user_id`, a 13-digit
  millisecond timestamp and a key that ends in another word, such as
  `telegram_rate_limit_ms`, do not match either.

A local run also checks a private list of names and paths that must never be
committed. Put one string per line in `~/.config/hl-guard-dev/traces.txt`, or
point `HL_GUARD_TRACES_FILE` at the file. CI has no such file. Without the
default file the tool prints a one-line note and runs the built-in rules only.
The note says when the default path is a broken symlink. The tool exits with
code 2 when the traces file it uses cannot be read or holds no traces.

A built-in rule can match ordinary code, such as a `/users/<id>` route. Add
`check-private-traces: allow` in a comment on that line to skip the built-in
rules for that line. A line ends at `\n`, `\r\n` or `\r`. A minified file
with no line breaks is one line, so a marker anywhere in it skips the built-in
rules for the whole file. The marker never skips the private list, and it does
not apply to file names.

The check targets accidental leaks, not deliberate hiding. It does not catch
these forms:

- Lookalike letters from other scripts, such as a Cyrillic `е` in a name.
- Invisible characters other than the soft hyphen, zero-width space, zero-width
  joiners, word joiner and byte order mark.
- URL, base64, hex, HTML-entity and backslash-hex encodings.
- Strings split across lines, string concatenation or digit groups.
- EVM addresses without the `0x` prefix, which look the same as git SHA-1s.
- Archives other than gzip, such as zip or bz2.

## Pull requests

- Use a Conventional Commits title, for example `fix(cli): reject unknown flags`.
- Keep each pull request small and focused.
- Never commit keys, seeds, wallet addresses, or tokens.

## Security

Report vulnerabilities privately. See [SECURITY.md](SECURITY.md).
