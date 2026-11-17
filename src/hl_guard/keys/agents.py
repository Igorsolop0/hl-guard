from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass

from hl_guard.keys.agent_key import ADDRESS, AgentKeyError, valid_until_ok
from hl_guard.server.info import InfoClient, InfoError


@dataclass(frozen=True)
class Listed:
    names: tuple[str | None, ...]
    valid_until_ms: int


def _bad(what: object) -> AgentKeyError:
    return AgentKeyError(f"the exchange's agent list is malformed: {json.dumps(what)[:200]}")


def _rows(info: InfoClient, master: str) -> list[object]:
    try:
        answer = info.post({"type": "extraAgents", "user": master})
    except InfoError as exc:
        raise AgentKeyError(f"cannot read the exchange's agent list: {exc}") from None
    if not isinstance(answer, list):
        raise _bad(answer)
    return answer


def _merge(listed: dict[str, Listed], address: str, name: object, until: int) -> None:
    seen = listed.get(address.lower(), Listed((), until))
    listed[address.lower()] = Listed(
        (*seen.names, name if isinstance(name, str) else None),
        max(seen.valid_until_ms, until),
    )


def listed_agents(info: InfoClient, master: str, ours: Collection[str]) -> dict[str, Listed]:
    wanted = {address.lower() for address in ours}
    listed: dict[str, Listed] = {}
    for row in _rows(info, master):
        if not isinstance(row, dict):
            continue
        address, until = row.get("address"), row.get("validUntil")
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            continue
        if not valid_until_ok(until):
            if address.lower() in wanted:
                raise _bad(row)
            continue
        _merge(listed, address, row.get("name"), until)
    return listed
