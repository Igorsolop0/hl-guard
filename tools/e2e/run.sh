#!/usr/bin/env bash
# The testnet end-to-end run: the init check, then one lane, and a ledger that
# verifies with the new records.
#
#   tools/e2e/run.sh --config ~/.config/hl-guard/serve.yaml --out DIR \
#     [--coin BTC] [--lane basic|tpsl]
#   tools/e2e/run.sh --dry-run [--lane basic|tpsl]
#
# basic: an entry that the policy rejects, an entry that you approve in a
# second terminal, its fill and a close.
# tpsl: an entry with a stop-loss and a take-profit that you approve, a check
# that both stops are live at the filled size, a close, and a check that no
# stop of the entry and no position is left.
#
# DIR gets the transcripts; keep it outside the repository, for example
# ~/hl-guard-e2e/run-1. --dry-run sends nothing and needs no config: it runs
# the lane's planner and judge offline on a made-up transcript.
#
# It refuses to run unless the policy, the data dir mode and the market read
# all say testnet. It never approves: when the entry waits, approve it with
# hl-guard approve in a second terminal. The coin must hold no position, since
# the run closes it right after the fill.
set -euo pipefail

usage() {
  echo "usage: tools/e2e/run.sh --config SERVE_CONFIG --out DIR [--coin COIN] [--lane basic|tpsl]" >&2
  echo "       tools/e2e/run.sh --dry-run [--lane basic|tpsl]" >&2
  exit 2
}

config=""
coin="BTC"
out=""
lane="basic"
dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) config="${2:-}"; shift 2 || usage ;;
    --coin) coin="${2:-}"; shift 2 || usage ;;
    --out) out="${2:-}"; shift 2 || usage ;;
    --lane) lane="${2:-}"; shift 2 || usage ;;
    --dry-run) dry=1; shift ;;
    *) usage ;;
  esac
done
case "$lane" in
  basic|tpsl) ;;
  *) usage ;;
esac
if [ "$dry" = 1 ]; then
  cd "$(dirname "$0")/../.."
  uv run python tools/e2e/e2e.py selftest --lane "$lane"
  echo "e2e dry run ($lane): PASS"
  exit 0
fi
[ -n "$config" ] || usage
[ -n "$coin" ] || usage
[ -n "$out" ] || usage
absolute() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$PWD" "$1" ;;
  esac
}
config="$(absolute "$config")"
out="$(absolute "$out")"
mkdir -p "$out"

cd "$(dirname "$0")/../.."
e2e=(uv run python tools/e2e/e2e.py)
serve=(uv run hl-guard serve --config "$config" --expect-network testnet)
echo "e2e: transcripts in $out"

step() { printf '\n== %s\n' "$*"; }

step "preflight: testnet only"
"${e2e[@]}" preflight --config "$config" --coin "$coin" | tee "$out/preflight.txt"
ledger="$(sed -n 's/^ledger //p' "$out/preflight.txt")"

step "init check: the agent is approved"
uv run hl-guard status --config "$config" --expect-network testnet

step "ledger before the run"
after_seq=0
if [ -e "$ledger" ]; then
  uv run hl-guard ledger verify "$ledger" | tee "$out/verify-before.txt"
  after_seq="$(sed -n 's/^verify ok: \([0-9]*\) records.*/\1/p' "$out/verify-before.txt")"
fi
echo "records before: $after_seq"

step "read the market and the positions"
cat > "$out/read.json" <<EOF
[{"tool": "markets", "arguments": {"coins": ["$coin"]}},
 {"tool": "positions", "arguments": {}}]
EOF
uv run python tools/drive.py "$out/read.json" -- "${serve[@]}" > "$out/read.txt"
"${e2e[@]}" scenario --config "$config" --coin "$coin" --lane "$lane" \
  --transcript "$out/read.txt" > "$out/scenario.json"
cat "$out/scenario.json"

step "lane $lane: entries, approval, fill, close"
ttl="$(sed -n 's/^approval_ttl_s //p' "$out/preflight.txt")"
set +e
uv run python tools/drive.py --timeout "$((ttl + 60))" "$out/scenario.json" -- "${serve[@]}" \
  | tee "$out/run.txt" | "${e2e[@]}" watch --config "$config"
drive_exit="${PIPESTATUS[0]}"
set -e
echo "drive exit $drive_exit"

step "ledger after the run"
uv run hl-guard ledger verify "$ledger"
uv run hl-guard ledger show "$ledger" --last 1000 > "$out/ledger.txt"

step "judge"
"${e2e[@]}" judge --config "$config" --coin "$coin" --lane "$lane" \
  --transcript "$out/run.txt" --ledger-lines "$out/ledger.txt" --after-seq "$after_seq"
echo "e2e: PASS"
