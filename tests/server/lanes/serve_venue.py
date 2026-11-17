"""Serve hl-guard over stdio against the loopback venue in ``tests/server/venue.py``.

    serve_venue.py <config> [--i-understand-autonomous]

``HL_GUARD_VENUE_LOG`` names a file that gets one JSON line per /exchange action.
``HL_GUARD_VENUE_HTTP_LOG`` lifts the /info budget and logs each /info round trip.
"""

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
from hl_guard.server.tools_write import ServeLockRefused, Writer
from server.fake_info import Answer, FakeInfo, point_testnet_at, serving
from server.venue import Venue

LOG_ENV = "HL_GUARD_VENUE_LOG"
TIMED_ENV = "HL_GUARD_VENUE_HTTP_LOG"


class TimedInfoClient(info.InfoClient):
    """Logs '<start ns> <end ns>' for each /info round trip, for the perf probe."""

    log: Path

    def _exchange(self, call: Any, url: str, data: bytes) -> tuple[int, bytes]:
        start = time.time_ns()
        try:
            return super()._exchange(call, url, data)
        finally:
            with self.log.open("a") as f:
                f.write(f"{start} {time.time_ns()}\n")


def logged(venue: Venue, log: Path) -> Any:
    def answer(body: dict[str, Any]) -> Answer:
        with log.open("a") as f:
            f.write(json.dumps(body["action"]) + "\n")
        return Venue.exchange(venue, body)

    return answer


if __name__ == "__main__":
    autonomous = "--i-understand-autonomous" in sys.argv[2:]
    venue = Venue()
    with serving(FakeInfo()) as fake:
        venue.install(fake)
        if log := os.environ.get(LOG_ENV):
            fake.exchange = logged(venue, Path(log))
        point_testnet_at(fake.url, fake.url.removesuffix("/info") + "/exchange")
        try:
            runtime = app.boot(Path(sys.argv[1]), None, autonomous=autonomous)
            writer = Writer.start(runtime)
        except (app.StartupRefused, ServeLockRefused) as exc:
            print(f"hl-guard serve refused: {exc}", file=sys.stderr)
            sys.exit(app.EXIT_REFUSED)
        if timed := os.environ.get(TIMED_ENV):
            unmetered = info.WeightBudget(runtime.config.budget_path, 10**9)
            runtime.info = TimedInfoClient(runtime.network, unmetered)
            runtime.info.log = Path(timed)
        anyio.run(app._serve, runtime, writer)
