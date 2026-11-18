# Releasing

[.github/workflows/release.yml](.github/workflows/release.yml) publishes
hl-guard to PyPI with trusted publishing: PyPI accepts a short-lived token
from GitHub Actions, so the repository stores no PyPI password or API token.

## One-time setup

A maintainer does this once, by hand:

1. On PyPI, add a trusted publisher for the project `hl-guard`: owner
   `Igorsolop0`, repository `hl-guard`, workflow `release.yml`, environment
   `pypi`.
2. On GitHub, create the environment `pypi` under **Settings**, then
   **Environments**, with a required reviewer. Each publish then waits for
   that person.

## Each release

1. Set the new version in `pyproject.toml`, run `uv lock` so that `uv.lock`
   has it too, and update `tests/test_version.py`, the "It prints" line of
   `docs/quickstart.md` and the version in the README status and in
   `docs/faq.md`. Move the "Unreleased" entries of `CHANGELOG.md` under the
   new version.
2. Run every check in [CONTRIBUTING.md](CONTRIBUTING.md).
3. Run the testnet end-to-end check, `tools/e2e/run.sh`, as its header
   describes. It needs a testnet agent key and a second terminal for the
   approval. Keep its transcripts. Do not release if it fails.
4. Merge to `main`, then tag the merge commit and push the tag:

<!-- docs-test: skip your-value -->
```sh
git tag -a v0.2.0 -m "hl-guard 0.2.0"
git push origin v0.2.0
```

5. Publish a GitHub release for the tag. The workflow starts on
   `release: published`. To publish a tag again without a new release, for
   example after a failed upload, run the workflow by hand from the
   **Actions** tab with the tag as input.

The workflow checks out the tag, builds the wheel and the source archive with
`uv build`, and installs the wheel in a fresh environment. It stops unless
`hl-guard --version` from that wheel prints the tag without its `v`. Then it
uploads both files with `pypa/gh-action-pypi-publish`, after the `pypi`
environment approves.
