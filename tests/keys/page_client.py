from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


def get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def post(url: str, body: bytes, headers: dict[str, str] | None = None) -> int:
    sent = {"Content-Type": "application/json"} | (headers or {})
    req = urllib.request.Request(url, data=body, method="POST", headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def step(page_url: str, path: str) -> int:
    return post(page_url.replace("/approve?", path + "?"), b"{}")


def claim_refused(page_url: str) -> bool:
    try:
        return step(page_url, "/claim") == 403
    except urllib.error.URLError:
        return True


def sending(page_url: str) -> bool:
    return step(page_url, "/claim") == 200 and step(page_url, "/send") == 200


def result(account: str, response: object) -> bytes:
    return json.dumps({"account": account, "response": response}).encode()


def embedded_typed_data(page_html: str) -> dict[str, Any]:
    found = re.search(r"const typedData = (\{.*?\});\n", page_html)
    assert found, "the page embeds the typed data"
    data: dict[str, Any] = json.loads(found.group(1))
    return data
