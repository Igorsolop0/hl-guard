import json
from pathlib import Path
from typing import Any

from hl_guard.ledger.ledger import GENESIS, Kind, _digest

TS0 = 1_760_000_000_000


def _fill(coin: str, side: str, size: str, price: str, fee: str) -> dict[str, Any]:
    return {"coin": coin, "side": side, "size": size, "price": price, "fee": fee}


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _rewrite(path: Path, lines: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    path.chmod(0o600)


def _rehash(line: dict[str, Any]) -> dict[str, Any]:
    keys = ("seq", "ts_ms", "kind", "payload", "prev")
    return {**line, "hash": _digest(*(line[k] for k in keys))}


def _forge(path: Path, kind: Kind, payload: dict[str, Any]) -> None:
    lines = _lines(path) if path.exists() else []
    head: dict[str, Any] = lines[-1] if lines else {"seq": 0, "ts_ms": TS0, "hash": GENESIS}
    line = {"seq": head["seq"] + 1, "ts_ms": head["ts_ms"], "kind": kind.value}
    _rewrite(path, [*lines, _rehash({**line, "payload": payload, "prev": head["hash"]})])
