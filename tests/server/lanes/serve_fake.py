from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hl_guard.server import app, info
from hl_guard.server.parse import FILLS_MAX
from server.fake_info import FakeInfo, point_testnet_at, recorded, serving

HTTP_LOG_ENV = "HL_GUARD_HTTP_LOG"
LIVE_SIZED_ENV = "HL_GUARD_FAKE_LIVE_SIZED"
REPLAY_ENV = "HL_GUARD_FAKE_REPLAY"
TESTNET_PERPS = 212
LOG = Path(os.environ[HTTP_LOG_ENV])


class TimedInfoClient(info.InfoClient):
    def _exchange(self, call: Any, url: str, data: bytes) -> tuple[int, bytes]:
        start = time.time_ns()
        try:
            return super()._exchange(call, url, data)
        finally:
            with LOG.open("a") as f:
                f.write(f"{start} {time.time_ns()}\n")


def live_sized() -> dict[str, Any]:
    meta, ctxs = json.loads(recorded("metaAndAssetCtxs"))
    names = [u["name"] for u in meta["universe"]]
    while len(meta["universe"]) < TESTNET_PERPS:
        i = len(meta["universe"])
        meta["universe"].append(meta["universe"][i % len(names)] | {"name": f"X{i}"})
        ctxs.append(ctxs[i % len(names)])
    fills = json.loads(recorded("userFills"))
    sized = {
        "metaAndAssetCtxs": json.dumps([meta, ctxs]).encode(),
        "userFills": json.dumps((fills * (FILLS_MAX // len(fills) + 1))[:FILLS_MAX]).encode(),
    }

    def answer(kind: str) -> Any:
        return lambda body: (200, sized[kind], {})

    return {kind: answer(kind) for kind in sized}


def replayed(spec: str) -> dict[str, Any]:
    kind, path = spec.split("=", 1)
    data = Path(path).read_bytes()
    return {kind: lambda body: (200, data, {})}


if __name__ == "__main__":
    answers: dict[str, Any] = live_sized() if os.environ.get(LIVE_SIZED_ENV) else {}
    if spec := os.environ.get(REPLAY_ENV):
        answers |= replayed(spec)
    with serving(FakeInfo(answers=answers)) as fake:
        point_testnet_at(fake.url)
        runtime = app.boot(Path(sys.argv[1]), None)
        unmetered = info.WeightBudget(runtime.config.budget_path, 10**9)
        runtime.info = TimedInfoClient(
            runtime.network, unmetered, timeout_s=runtime.config.info_timeout_s
        )
        anyio.run(app._serve, runtime, None)
