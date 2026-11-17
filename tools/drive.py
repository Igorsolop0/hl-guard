from __future__ import annotations

import argparse
import json
import math
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage

if TYPE_CHECKING:
    from mcp.shared._stream_protocols import ReadStream, WriteStream

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_HARNESS_ERROR = 2

T = TypeVar("T")


class Closable(Protocol):
    async def aclose(self) -> None: ...


class HarnessError(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Run:
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Wait:
    seconds: float


Step = ToolCall | Run | Wait


def _step(i: int, item: object) -> Step:
    if isinstance(item, dict) and set(item) == {"run"}:
        argv = item["run"]
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise HarnessError(f"scenario call {i}: 'run' must be a non-empty list of strings")
        return Run(tuple(argv))
    if isinstance(item, dict) and set(item) == {"wait"}:
        seconds = item["wait"]
        if type(seconds) not in (int, float) or not 0 <= seconds <= 3600:
            raise HarnessError(f"scenario call {i}: 'wait' must be 0 to 3600 seconds")
        return Wait(float(seconds))
    if not isinstance(item, dict) or set(item) - {"tool", "arguments"}:
        raise HarnessError(
            f"scenario call {i}: expected keys 'tool' and 'arguments', or 'run', or 'wait'"
        )
    tool, arguments = item.get("tool"), item.get("arguments", {})
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        raise HarnessError(f"scenario call {i}: 'tool' must be a string, 'arguments' an object")
    return ToolCall(tool, arguments)


def parse_scenario(raw: object) -> list[Step]:
    if not isinstance(raw, list):
        raise HarnessError("scenario must be a JSON list")
    return [_step(i, item) for i, item in enumerate(raw)]


def result_id(result: Any) -> str | None:
    for item in getattr(result, "content", []):
        try:
            value = json.loads(getattr(item, "text", ""))
        except ValueError:
            continue
        found = value.get("id") if isinstance(value, dict) else None
        if isinstance(found, str):
            return found
    return None


async def run_local(step: Run, last_id: str | None) -> None:
    if any("{id}" in a for a in step.argv) and last_id is None:
        raise HarnessError("a run step names {id} but no tool result had an id")
    argv = [a.replace("{id}", last_id or "") for a in step.argv]
    print("$ " + " ".join(argv), flush=True)
    done = await anyio.run_process(argv, check=False, stderr=subprocess.STDOUT)
    for line in done.stdout.decode(errors="replace").splitlines():
        print(f"| {line}", flush=True)
    print(f"run exit {done.returncode}", flush=True)


def emit(direction: str, method: str, payload: object) -> None:
    print(f"{direction} {method} {json.dumps(payload, sort_keys=True)}", flush=True)


def emit_message(direction: str, item: SessionMessage, *, responses: bool) -> None:
    message = item.message.model_dump(mode="json", by_alias=True, exclude_none=True)
    if "method" in message:
        emit(direction, message["method"], message.get("params", {}))
    elif responses:
        emit(
            direction,
            "response",
            {k: message[k] for k in ("id", "result", "error") if k in message},
        )


class Tap:
    inner: Closable

    def __init__(self, inner: Closable) -> None:
        self.inner = inner

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        await self.aclose()
        return None


class PrintingWriteStream(Tap):
    inner: WriteStream[SessionMessage]

    async def send(self, item: SessionMessage, /) -> None:
        emit_message(">>", item, responses=True)
        await self.inner.send(item)


class PrintingReadStream(Tap):
    inner: ReadStream[SessionMessage | Exception]

    @property
    def last_context(self) -> object:
        return getattr(self.inner, "last_context", None)

    def show(self, item: SessionMessage | Exception) -> SessionMessage | Exception:
        if isinstance(item, SessionMessage):
            emit_message("<<", item, responses=False)
        return item

    async def receive(self) -> SessionMessage | Exception:
        return self.show(await self.inner.receive())

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        return self.show(await self.inner.__anext__())


async def within(timeout: float, label: str, step: Awaitable[T]) -> T:
    try:
        with anyio.fail_after(timeout):
            return await step
    except TimeoutError:
        raise HarnessError(f"{label} timed out after {timeout:g} s") from None


async def cancel_on_signal(signals: AsyncIterator[int], scope: anyio.CancelScope) -> None:
    async for _ in signals:
        scope.cancel()


async def drive(server: StdioServerParameters, calls: list[Step], timeout: float) -> int:
    with anyio.open_signal_receiver(signal.SIGTERM) as signals, anyio.CancelScope() as scope:
        async with anyio.create_task_group() as group:
            group.start_soon(cancel_on_signal, signals, scope)
            code = await run(server, calls, timeout)
            group.cancel_scope.cancel()
    if scope.cancel_called:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise HarnessError("terminated by SIGTERM")
    return code


async def run(server: StdioServerParameters, calls: list[Step], timeout: float) -> int:
    failed = 0
    last_id: str | None = None
    async with (
        stdio_client(server) as (read, write),
        ClientSession(
            PrintingReadStream(read), PrintingWriteStream(write), read_timeout_seconds=timeout
        ) as session,
    ):
        init = await within(timeout, "initialize", session.initialize())
        emit("<<", "initialize", init.model_dump(mode="json", by_alias=True, exclude_none=True))
        print("initialized", flush=True)

        tools = await within(timeout, "tools/list", session.list_tools())
        emit("<<", "tools/list", tools.model_dump(mode="json", by_alias=True, exclude_none=True))
        print("tools: " + ", ".join(t.name for t in tools.tools), flush=True)

        for call in calls:
            if isinstance(call, Wait):
                print(f"wait {call.seconds:g} s", flush=True)
                await anyio.sleep(call.seconds)
                continue
            if isinstance(call, Run):
                await within(timeout, f"run {call.argv[0]}", run_local(call, last_id))
                continue
            arguments = {
                k: v.replace("{id}", last_id) if isinstance(v, str) and last_id else v
                for k, v in call.arguments.items()
            }
            result = await within(
                timeout, f"tools/call {call.tool}", session.call_tool(call.tool, arguments)
            )
            emit(
                "<<", "tools/call", result.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            failed += result.is_error
            last_id = result_id(result) or last_id
    return EXIT_TOOL_ERROR if failed else EXIT_OK


def innermost(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite number above 0, not {value}")
    return number


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        usage="%(prog)s [--timeout SECONDS] scenario -- server-command [args ...]",
        description="Run a scenario of MCP tool calls against a stdio server and print the "
        'transcript. A step can also run a local command ({"run": [argv]}, where {id} is '
        'the id of the last tool result) or wait ({"wait": seconds}). Exit 0 when every '
        "call succeeds, 1 when a tool returns an error, 2 on a harness error.",
    )
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--timeout", type=seconds, default=30.0, help="seconds per request")
    split = argv.index("--") if "--" in argv else len(argv)
    args, command = parser.parse_args(argv[:split]), argv[split + 1 :]
    if not command:
        parser.error("give the server command after --")

    try:
        calls = parse_scenario(json.loads(args.scenario.read_text()))
        server = StdioServerParameters(command=command[0], args=command[1:])
        return anyio.run(drive, server, calls, args.timeout)
    except Exception as exc:
        error = innermost(exc)
        kind = "" if isinstance(error, HarnessError) else f"{type(error).__name__}: "
        print(f"drive: harness error: {kind}{error}", file=sys.stderr)
        return EXIT_HARNESS_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
