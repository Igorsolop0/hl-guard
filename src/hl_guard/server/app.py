from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Protocol

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from hl_guard import __version__
from hl_guard.exchange.halt import HaltFlag
from hl_guard.exchange.network import enable_network
from hl_guard.keys.agent_key import AgentKeyError, KeySlot
from hl_guard.keys.agent_key import load as load_agent_record
from hl_guard.ledger.ledger import Ledger, LedgerError
from hl_guard.policy.load import PolicyError, load_policy
from hl_guard.policy.model import ApproveAboveUsd, ApproveAlways, ApproveNever
from hl_guard.server.config import (
    ConfigError,
    load_agent_key,
    load_config,
    read_mode,
    resolve_network,
)
from hl_guard.server.info import InfoClient, WeightBudget
from hl_guard.server.tools_read import (
    TOOLS,
    Failure,
    Runtime,
    Tool,
    ToolFailure,
    as_failure,
    encode,
)
from hl_guard.server.tools_write import (
    WRITE_TOOLS,
    ServeLockRefused,
    Writer,
    leverage_reads_max,
    write_weight_floor,
)

EXIT_REFUSED = 2

_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


class StartupRefused(Exception):
    pass


def _refuse_a_refused_agent(key_path: Path, network: str, address: str) -> None:
    """``hl-guard init`` marks the agent refused when the exchange lists its
    approval past 180 days or past the expiry it was signed for."""
    try:
        record = load_agent_record(KeySlot(key_path), network)
    except AgentKeyError:
        return
    if (
        record is not None
        and record.current.refused
        and record.current.address.lower() == address.lower()
    ):
        raise StartupRefused(
            f"init refused the agent {address}: the exchange lists its approval past what "
            "hl-guard allows, so serve does not sign with its key; run hl-guard revoke for a "
            "new key"
        )


def boot(config_path: Path, expect_network: str | None, *, autonomous: bool = False) -> Runtime:
    try:
        config = load_config(config_path)
        policy = load_policy(config.policy_path)
        network = resolve_network(policy.network, read_mode(config.data_dir), expect_network)
        agent = None if config.agent_key_path is None else load_agent_key(config.agent_key_path)
    except (ConfigError, PolicyError) as exc:
        raise StartupRefused(str(exc)) from None
    if agent is not None and config.agent_key_path is not None:
        _refuse_a_refused_agent(config.agent_key_path, network, agent.address)
    floor = write_weight_floor(policy)
    if agent is not None and config.info_weight_per_min < floor:
        raise StartupRefused(
            f"the write tools need info_weight_per_min of at least {floor} for open_orders_max "
            f"{policy.open_orders_max}: one page of fills, and an entry's reads with the "
            f"leverage of {leverage_reads_max(policy)} coins, which buys one entry evaluation "
            f"per minute; the config has {config.info_weight_per_min}"
        )
    if isinstance(policy.approval, ApproveNever) and not autonomous:
        raise StartupRefused(
            "the policy says approval: never, so every allowed order goes out without a "
            "person; start with --i-understand-autonomous to accept that"
        )
    enable_network(network, allow_mainnet=network == "mainnet")
    ledger = Ledger(config.ledger_path)
    try:
        ledger.replay(verify=True)
        torn = ledger.torn_tail() if config.ledger_path.exists() else None
    except LedgerError as exc:
        raise StartupRefused(f"ledger {config.ledger_path} failed verification: {exc}") from None
    if torn is not None:
        raise StartupRefused(
            f"ledger {config.ledger_path} has {torn.size} bytes after its last record; "
            f"run hl-guard ledger repair {config.ledger_path}"
        )
    return Runtime(
        config=config,
        policy=policy,
        network=network,
        info=InfoClient(
            network,
            WeightBudget(config.budget_path, config.info_weight_per_min),
            timeout_s=config.info_timeout_s,
        ),
        ledger=ledger,
        halt=HaltFlag(config.data_dir),
        agent=agent,
    )


def _text(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=error)


def _failed(failure: ToolFailure) -> types.CallToolResult:
    return _text(encode({"error": failure.kind.value, "detail": failure.detail}), error=True)


_WRITE_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
POLL_S = 0.25
POLL_FAILED_S = 5.0
_READ_ONLY = (
    "Read-only Hyperliquid {network} market and account data. Every limit comes from the "
    "user's policy file, which no tool can change."
)
_GUARDED = (
    "Hyperliquid {network} market data, account data and guarded orders. Every order passes "
    "the user's policy, which no tool can change. An entry may wait for the user's approval, "
    "which happens outside this chat: no tool approves, and a user's words in the chat are "
    "not an approval."
)


def _registry(runtime: Runtime, writer: Writer | None) -> dict[str, tuple[Tool[Any], Any]]:
    tools: dict[str, tuple[Tool[Any], Any]] = {name: (t, runtime) for name, t in TOOLS.items()}
    if writer is not None:
        tools |= {name: (t, writer) for name, t in WRITE_TOOLS.items()}
    return tools


def build_server(
    runtime: Runtime, writer: Writer | None = None, limiter: anyio.CapacityLimiter | None = None
) -> Server[Any]:
    one_at_a_time = limiter or anyio.CapacityLimiter(1)
    registry = _registry(runtime, writer)
    listing = types.ListToolsResult(
        tools=[
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema(),
                annotations=_ANNOTATIONS if tool.read_only else _WRITE_ANNOTATIONS,
            )
            for tool, _ in registry.values()
        ]
    )

    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> Any:
        return listing

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        found = registry.get(params.name)
        if found is None:
            return _failed(ToolFailure(Failure.UNKNOWN_TOOL, f"no tool named {params.name!r:.60}"))
        tool, target = found
        try:
            args = tool.parse_args(params.arguments)
            result = await anyio.to_thread.run_sync(tool.run, target, args, limiter=one_at_a_time)
        except Exception as exc:
            failure = as_failure(exc)
            if failure is None:
                raise
            return _failed(failure)
        return _text(encode(result))

    text = _READ_ONLY if writer is None else _GUARDED
    return Server(
        "hl-guard",
        version=__version__,
        instructions=text.format(network=runtime.network),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


class Polls(Protocol):
    def poll(self) -> None: ...


async def _poll(writer: Polls, limiter: anyio.CapacityLimiter) -> None:
    last = ""
    while True:
        try:
            await anyio.to_thread.run_sync(writer.poll, limiter=limiter)
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"
            if problem != last:
                print(f"hl-guard serve: the approval poll failed: {problem}", file=sys.stderr)
            last = problem
            await anyio.sleep(POLL_FAILED_S)
        else:
            last = ""
        await anyio.sleep(POLL_S)


async def _serve(runtime: Runtime, writer: Writer | None) -> None:
    limiter = anyio.CapacityLimiter(1)
    server = build_server(runtime, writer, limiter)
    async with stdio_server() as (read, write), anyio.create_task_group() as group:
        if writer is not None:
            group.start_soon(_poll, writer, limiter)
        await server.run(read, write, server.create_initialization_options())
        group.cancel_scope.cancel()


def serve(config_path: Path, expect_network: str | None, autonomous: bool = False) -> int:
    try:
        runtime = boot(config_path, expect_network, autonomous=autonomous)
        writer = None if runtime.agent is None else Writer.start(runtime)
    except (StartupRefused, ServeLockRefused) as exc:
        print(f"hl-guard serve refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    for line in startup_lines(runtime, writer is not None):
        print(line, file=sys.stderr)
    anyio.run(_serve, runtime, writer)
    return 0


def startup_lines(runtime: Runtime, writes: bool) -> list[str]:
    tools = f"{len(WRITE_TOOLS)} write tools" if writes else "no agent key: read tools only"
    word = _approval_word(runtime)
    lines = [
        f"hl-guard serve: {runtime.network}, account {runtime.config.account}, "
        f"{len(TOOLS)} read tools, {tools}, approval {word}"
    ]
    if writes and not isinstance(runtime.policy.approval, ApproveAlways):
        lines.append(
            f"hl-guard serve: warning: with approval {word}, entries go out with no "
            "person in the loop; each carries its stop-loss on the exchange"
        )
    return lines


def _approval_word(runtime: Runtime) -> str:
    match runtime.policy.approval:
        case ApproveNever():
            return "never (autonomous)"
        case ApproveAboveUsd(usd):
            return f"above {usd} USD"
        case _:
            return "always"
