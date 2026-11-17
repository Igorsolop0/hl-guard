"""An append-only JSON Lines ledger with a SHA-256 hash chain.

Each line holds ``seq``, ``ts_ms``, ``kind``, ``payload``, ``prev`` and
``hash``. ``hash`` covers the other five fields in canonical JSON, and ``prev``
is the previous line's ``hash``. Editing, dropping or reordering a line breaks
the chain from that line on. The hash covers the parsed fields, not the bytes,
so a rewrite that only changes key order or spacing still verifies. ``seq``
is the line number, and ``ts_ms`` is after 2020-01-01 and never goes back.
An explicit ``ts_ms`` may be at most one day ahead of the clock when it is
written. Reads do not compare it with the clock, so a clock that steps back
cannot make a committed line fail, and a latch is always recordable.

``append`` checks each new line with the same checks reads apply, so it
never commits a line that a later replay rejects. Those checks do not depend
on the process: a line is at most 256 KiB, its payload nests at most 32
levels, an integer is in the range -2**63 to 2**64-1, no object repeats a key,
and replay arithmetic runs in a fixed decimal context. A latch is never refused for its
size: ``append`` cuts its rule and detail to 1000 characters.

Dropping whole lines from the end leaves a shorter valid chain. Only an anchor
kept elsewhere detects it: record the count and head hash that
``hl-guard ledger verify`` prints, and compare them later.

A line counts as written once its newline is on disk. Bytes after the last
newline come from a crash during a write. Reads skip them and ``verify``
reports them. ``repair`` and ``append`` drop them, unless they hold the whole
next record and only its newline is missing: then they add the newline.

The file must stay owner-only (mode 0600) on a local disk: ``flock`` and
``fsync`` are not reliable on network file systems.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple

GENESIS = "0" * 64
_TAIL_CHUNK = 64 * 1024
_FIELDS = frozenset({"seq", "ts_ms", "kind", "payload", "prev", "hash"})
_TS_MIN_MS = 1_577_836_800_000
_TS_AHEAD_MS = 24 * 60 * 60 * 1000
_DECIMAL_MIN = Decimal("1e-30")
_DECIMAL_MAX = Decimal("1e30")
_DECIMAL_CONTEXT = Context(
    prec=28, rounding=ROUND_HALF_EVEN, Emin=-999_999, Emax=999_999, capitals=1, clamp=0, traps=[]
)
_LINE_BYTES_MAX = 256 * 1024
_PAYLOAD_DEPTH_MAX = 32
_DAY_MS = 86_400_000
_INT_MIN = -(2**63)
_INT_MAX = 2**64 - 1
_INT_DIGITS_MAX = len(str(_INT_MAX))
_LATCH_TEXT_MAX = 1000
_CUT = " [truncated]"
_TOO_LONG = f"the line is longer than {_LINE_BYTES_MAX} bytes"
_TOO_DEEP = f"the payload nests deeper than {_PAYLOAD_DEPTH_MAX} levels"
_OUT_OF_RANGE = "an integer is outside the range -2**63 to 2**64-1"
_STRING_OR_BRACKET = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?|([\[\]{}])')


class Kind(StrEnum):
    PROPOSAL = "proposal"
    DECISION = "decision"
    APPROVAL = "approval"
    ORDER = "order"
    ACK = "ack"
    FILL = "fill"
    FUNDING = "funding"
    CANCEL = "cancel"
    HALT = "halt"
    UNHALT = "unhalt"
    LATCH = "latch"
    UNLATCH = "unlatch"
    STATE_CHECKPOINT = "state_checkpoint"
    DAY_START = "day_start"
    FILLS_READ = "fills_read"
    ERROR = "error"


class LedgerError(Exception):
    pass


class LedgerAccessError(LedgerError):
    """The file cannot be opened safely: no permission, another owner, or group or other bits."""


class LedgerTamperError(LedgerError):
    """Line ``line`` of the file breaks the chain or a checkpoint. ``line`` is
    None when only the tail was read, so the line number is unknown."""

    def __init__(self, line: int | None, reason: str) -> None:
        super().__init__(reason if line is None else f"line {line}: {reason}")
        self.line = line
        self.reason = reason


@dataclass(frozen=True)
class Record:
    seq: int
    ts_ms: int
    kind: Kind
    payload: Mapping[str, Any]
    prev: str
    hash: str


@dataclass(frozen=True)
class TornTail:
    """``size`` bytes follow the last newline. ``is_record`` means they hold the
    whole next record and only its newline is missing."""

    size: int
    is_record: bool


def _canonical(body: Mapping[str, Any]) -> bytes:
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _digest(seq: int, ts_ms: int, kind: str, payload: Mapping[str, Any], prev: str) -> str:
    body = {"seq": seq, "ts_ms": ts_ms, "kind": kind, "payload": payload, "prev": prev}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ts_problem(ts_ms: int, prev_ts_ms: int) -> str | None:
    if ts_ms < _TS_MIN_MS:
        return f"ts_ms {ts_ms} is before 2020-01-01"
    if ts_ms < prev_ts_ms:
        return f"ts_ms {ts_ms} is before the previous record's {prev_ts_ms}"
    return None


class _Malformed(ValueError):
    pass


def _nests_too_deep(text: str) -> bool:
    if text.count("[") + text.count("{") <= _PAYLOAD_DEPTH_MAX + 1:
        return False
    depth = 0
    for bracket in "".join(_STRING_OR_BRACKET.findall(text)):
        depth += 1 if bracket in "[{" else -1
        if depth > _PAYLOAD_DEPTH_MAX + 1:
            return True
    return False


def _bounded_int(digits: str) -> int:
    if len(digits.lstrip("-")) > _INT_DIGITS_MAX:
        raise _Malformed(_OUT_OF_RANGE)
    value = int(digits)
    if not _INT_MIN <= value <= _INT_MAX:
        raise _Malformed(_OUT_OF_RANGE)
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _Malformed("an object repeats a key")
    return obj


def _load(raw: bytes) -> Any:
    if len(raw) > _LINE_BYTES_MAX:
        raise _Malformed(_TOO_LONG)
    text = raw.decode()
    if _nests_too_deep(text):
        raise _Malformed(_TOO_DEEP)
    return json.loads(text, parse_int=_bounded_int, object_pairs_hook=_unique_keys)


def _payload_problem(payload: Mapping[str, Any]) -> str | None:
    pending: list[tuple[Any, int]] = [(payload, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, dict):
            children: Iterable[Any] = value.values()
        elif isinstance(value, list | tuple):
            children = value
        else:
            if isinstance(value, int) and not _INT_MIN <= value <= _INT_MAX:
                return _OUT_OF_RANGE
            continue
        if depth > _PAYLOAD_DEPTH_MAX:
            return _TOO_DEEP
        pending.extend((child, depth + 1) for child in children)
    return None


def _decode_and_check_hash(raw: bytes, line: int | None) -> Record:
    if b"\r" in raw:
        raise LedgerTamperError(line, "a raw carriage return is inside the line")
    try:
        obj = _load(raw)
        if not isinstance(obj, dict) or set(obj) != _FIELDS:
            raise LedgerTamperError(line, "not a ledger record")
        seq, ts_ms, kind, payload, prev = (
            obj[k] for k in ("seq", "ts_ms", "kind", "payload", "prev")
        )
        if type(seq) is not int or type(ts_ms) is not int:
            raise LedgerTamperError(line, "seq and ts_ms must be integers")
        if kind not in Kind.__members__.values() or not isinstance(payload, dict):
            raise LedgerTamperError(line, f"unknown kind {kind!r} or payload not an object")
        if not isinstance(prev, str) or obj["hash"] != _digest(seq, ts_ms, kind, payload, prev):
            raise LedgerTamperError(line, "hash does not match the line's contents")
    except _Malformed as exc:
        raise LedgerTamperError(line, str(exc)) from None
    except ValueError:
        raise LedgerTamperError(line, "not valid JSON") from None
    return Record(seq, ts_ms, Kind(kind), payload, prev, obj["hash"])


def _follow_problem(record: Record, before: Record | None) -> str | None:
    seq = before.seq + 1 if before else 1
    if record.seq != seq:
        return f"seq {record.seq} is not the line number {seq}"
    if record.prev != (before.hash if before else GENESIS):
        return "prev does not match the previous line's hash"
    return _ts_problem(record.ts_ms, before.ts_ms if before else _TS_MIN_MS)


def _parse_line(raw: bytes, line: int, before: Record | None) -> Record:
    record = _decode_and_check_hash(raw, line)
    problem = _follow_problem(record, before)
    if problem is not None:
        raise LedgerTamperError(line, problem)
    return record


def _parse_committed(data: bytes) -> Iterator[Record]:
    before = None
    *lines, _torn = data.split(b"\n")
    for line, raw in enumerate(lines, start=1):
        before = _parse_line(raw, line, before)
        yield before


def _fold(records: Iterable[Record], verify: bool) -> tuple[list[Record], _State]:
    state = _State(verify)
    out = []
    for record in records:
        state.apply(record)
        out.append(record)
    return out, state


def day_start_equity(records: Iterable[Record], day: int) -> Decimal | None:
    """The equity recorded at the start of UTC day ``day`` (ms epoch // 86400000)."""
    return _fold(records, verify=False)[1].day_starts.get(day)


def active_latch(records: Iterable[Record]) -> Record | None:
    """The latest latch record, unless an unlatch record follows it."""
    return _fold(records, verify=False)[1].latch


def clock_warning(head: Record | None) -> str | None:
    ahead = head.ts_ms - _now_ms() if head else 0
    if ahead <= _TS_AHEAD_MS:
        return None
    hours, tenth = divmod((ahead + 180_000) // 360_000, 10)
    return (
        f"clock warning: the newest record is {hours}.{tenth} h ahead of this clock; "
        "check the system clock"
    )


def _line_start(fd: int, end: int) -> int:
    pos = end
    while pos > 0:
        start = max(0, pos - _TAIL_CHUNK)
        chunk = os.pread(fd, pos - start, start)
        nl = chunk.rfind(b"\n")
        if nl >= 0:
            return start + nl + 1
        pos = start
    return 0


def _read_all(fd: int) -> bytes:
    size = os.fstat(fd).st_size
    chunks = []
    pos = 0
    while pos < size:
        chunk = os.pread(fd, size - pos, pos)
        if not chunk:
            break
        chunks.append(chunk)
        pos += len(chunk)
    return b"".join(chunks)


class _Head(NamedTuple):
    record: Record | None
    end: int


def _tail_record(fd: int, start: int, end: int, path: Path) -> Record:
    try:
        return _decode_and_check_hash(os.pread(fd, end - start, start)[:-1], None)
    except LedgerTamperError as exc:
        raise LedgerTamperError(
            None, f"{path}: the last line is not a ledger record: {exc.reason}"
        ) from None


def _tail_checked_head(fd: int, path: Path) -> _Head:
    end = _line_start(fd, os.fstat(fd).st_size)
    if end == 0:
        return _Head(None, 0)
    start = _line_start(fd, end - 1)
    before = _tail_record(fd, _line_start(fd, start - 1), start, path) if start else None
    last = _tail_record(fd, start, end, path)
    problem = _follow_problem(last, before)
    if problem is not None:
        raise LedgerTamperError(
            None, f"{path}: the last line does not follow the line before it: {problem}"
        )
    return _Head(last, end)


def _torn_tail(fd: int, head: _Head) -> TornTail | None:
    size = os.fstat(fd).st_size - head.end
    if size == 0:
        return None
    line = head.record.seq + 1 if head.record else 1
    try:
        _parse_line(os.pread(fd, size, head.end), line, head.record)
    except LedgerTamperError:
        return TornTail(size, is_record=False)
    return TornTail(size, is_record=True)


def _settle_tail(fd: int, path: Path) -> tuple[_Head, TornTail | None]:
    head = _tail_checked_head(fd, path)
    torn = _torn_tail(fd, head)
    if torn is None:
        return head, None
    if torn.is_record:
        os.write(fd, b"\n")
        os.fsync(fd)
        return _tail_checked_head(fd, path), torn
    os.ftruncate(fd, head.end)
    os.fsync(fd)
    return head, torn


def _next_ts_ms(head: Record | None, ts_ms: int | None) -> int:
    now = _now_ms()
    if ts_ms is None:
        return max(now, head.ts_ms if head else _TS_MIN_MS)
    if ts_ms > now + _TS_AHEAD_MS:
        raise ValueError(f"ts_ms {ts_ms} is more than one day ahead of the clock")
    return ts_ms


def _write(
    fd: int,
    head: Record | None,
    kind: Kind,
    payload: Mapping[str, Any],
    ts_ms: int,
    state: _State,
) -> Record:
    if (problem := _payload_problem(payload)) is not None:
        raise ValueError(f"{kind} record refused: {problem}")
    seq, prev = (head.seq + 1, head.hash) if head else (1, GENESIS)
    digest = _digest(seq, ts_ms, kind.value, payload, prev)
    body = {"seq": seq, "ts_ms": ts_ms, "kind": kind.value, "payload": payload, "prev": prev}
    raw = _canonical({**body, "hash": digest})
    try:
        record = _parse_line(raw, seq, head)
        state.apply(record)
    except LedgerTamperError as exc:
        raise ValueError(f"{kind} record refused: {exc.reason}") from None
    line = memoryview(raw + b"\n")
    while line:
        line = line[os.write(fd, line) :]
    os.fsync(fd)
    return record


def _open(path: Path, write: bool) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND if write else os.O_RDONLY
    try:
        fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as exc:
        raise LedgerAccessError(f"cannot open ledger {path}: {exc.strerror}") from None
    st = os.fstat(fd)
    problem = None
    if not stat.S_ISREG(st.st_mode):
        problem = "is not a regular file"
    elif st.st_uid != os.geteuid():
        problem = f"belongs to uid {st.st_uid}, not you"
    elif st.st_mode & 0o077:
        problem = f"has mode {stat.S_IMODE(st.st_mode):04o}; run chmod 600 {path}"
    if problem is not None:
        os.close(fd)
        raise LedgerAccessError(f"ledger {path} {problem}")
    fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
    return fd


_NEEDS_HISTORY = frozenset({Kind.STATE_CHECKPOINT, Kind.DAY_START})


def _appendable(kind: Kind, payload: Mapping[str, Any], ts_ms: int | None) -> dict[str, Any]:
    if not isinstance(kind, Kind):
        raise ValueError(f"kind {kind!r} is not a Kind")
    if not isinstance(payload, Mapping):
        raise ValueError(f"{kind} payload must be a mapping, got {type(payload).__name__}")
    if kind is Kind.UNLATCH:
        raise ValueError("an unlatch record is written only by Tail.unlatch")
    if kind is Kind.LATCH and ts_ms is not None:
        raise ValueError("a latch record takes its ts_ms from the clock")
    if kind is Kind.LATCH:
        return {key: _cut(value) for key, value in payload.items()}
    return dict(payload)


def _cut(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _LATCH_TEXT_MAX:
        return value[: _LATCH_TEXT_MAX - len(_CUT)] + _CUT
    return value


@dataclass(frozen=True)
class Unlatched:
    latch: Record
    record: Record


class Ledger:
    """One file, many writers: appends take an exclusive ``flock``, reads a shared one."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def torn_tail(self) -> TornTail | None:
        """Read-only: what follows the last committed record, if anything."""
        fd = _open(self.path, write=False)
        try:
            return _torn_tail(fd, _tail_checked_head(fd, self.path))
        finally:
            os.close(fd)

    def repair(self) -> TornTail | None:
        """Drop torn bytes, or add the newline a whole last record lacks."""
        fd = _open(self.path, write=True)
        try:
            return _settle_tail(fd, self.path)[1]
        finally:
            os.close(fd)

    def append(self, kind: Kind, payload: Mapping[str, Any], ts_ms: int | None = None) -> Record:
        """``payload`` must be JSON: put Decimals in as strings. A checkpoint
        must equal the replay of every line before it."""
        payload = _appendable(kind, payload, ts_ms)
        fd = _open(self.path, write=True)
        try:
            head, _ = _settle_tail(fd, self.path)
            if kind in _NEEDS_HISTORY:
                state = _fold(_parse_committed(_read_all(fd)), verify=True)[1]
            else:
                state = _State(verify=True)
            return _write(fd, head.record, kind, payload, _next_ts_ms(head.record, ts_ms), state)
        finally:
            os.close(fd)

    def records(self) -> Iterator[Record]:
        """Every committed record in order, each checked against the chain."""
        if not self.path.exists():
            return
        fd = _open(self.path, write=False)
        try:
            data = _read_all(fd)
        finally:
            os.close(fd)
        yield from _parse_committed(data)

    def replay(self, verify: bool = True) -> list[Record]:
        """With ``verify``, also check each ``state_checkpoint`` against the
        reduction of the records before it."""
        return _fold(self.records(), verify)[0]

    def reduce(self) -> dict[str, Any]:
        return _fold(self.records(), verify=False)[1].snapshot()


class Tail:
    """The committed records of one ledger, read incrementally.

    Each call parses only the lines appended since the last call and chains
    them to the last record seen, so a long-running reader pays for each line
    once. A rewrite of a line it already read shows only to a full read such
    as ``hl-guard ledger verify``. A file that shrank is read again from the start.
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._records: list[Record] = []
        self._end = 0
        self._folded_state = _State(verify=True)
        self._folded_count = 0

    def records(self) -> list[Record]:
        if not self.ledger.path.exists():
            return []
        fd = _open(self.ledger.path, write=False)
        try:
            data = self._unread(fd)
        finally:
            os.close(fd)
        self._parse(data)
        return list(self._records)

    def append(
        self,
        kind: Kind,
        payload: Mapping[str, Any],
        *,
        ts_ms: int | None = None,
        after_seq: int = 0,
        check: Callable[[list[Record], int], None] | None = None,
    ) -> Record:
        """Append under the exclusive lock once ``check`` has seen every record
        with a seq above ``after_seq`` and the ts_ms the record will carry, and
        not refused by raising. The lock is held while the lines this tail has
        not read yet are parsed and folded, not the whole file."""
        if kind is Kind.STATE_CHECKPOINT:
            raise ValueError(f"a {kind} record needs a verified replay; use Ledger.append")
        payload = _appendable(kind, payload, ts_ms)
        history = kind in _NEEDS_HISTORY
        if history:
            self._folded_copy()
        fd = _open(self.ledger.path, write=True)
        try:
            head, _ = self._catch_up(fd)
            stamp = _next_ts_ms(head.record, ts_ms)
            if check is not None:
                seen = after_seq if after_seq <= len(self._records) else 0
                check(self._records[seen:], stamp)
            state = self._folded_copy() if history else _State(verify=True)
            return _write(fd, head.record, kind, payload, stamp, state)
        finally:
            os.close(fd)

    def unlatch(self) -> tuple[Unlatched | None, TornTail | None]:
        """Find the latch and write the unlatch under one exclusive lock, so a
        latch recorded since this tail last read cannot be cleared unseen."""
        self._folded_copy()
        fd = _open(self.ledger.path, write=True)
        try:
            head, torn = self._catch_up(fd)
            state = self._folded_copy()
            latch = state.latch
            if latch is None:
                return None, torn
            payload = {"clears_seq": latch.seq}
            record = _write(
                fd, head.record, Kind.UNLATCH, payload, _next_ts_ms(head.record, None), state
            )
            return Unlatched(latch, record), torn
        finally:
            os.close(fd)

    def _catch_up(self, fd: int) -> tuple[_Head, TornTail | None]:
        head, torn = _settle_tail(fd, self.ledger.path)
        if head.end < self._end:
            raise LedgerTamperError(None, f"{self.ledger.path} changed under its reader")
        self._parse(self._unread(fd))
        last = self._records[-1] if self._records else None
        if last != head.record:
            raise LedgerTamperError(None, f"{self.ledger.path} changed under its reader")
        return head, torn

    def _folded_copy(self) -> _State:
        for record in self._records[self._folded_count :]:
            self._folded_state.apply(record)
        self._folded_count = len(self._records)
        return self._folded_state.copy()

    def _unread(self, fd: int) -> bytes:
        size = os.fstat(fd).st_size
        if size < self._end:
            self._records, self._end = [], 0
            self._folded_state, self._folded_count = _State(verify=True), 0
        return os.pread(fd, size - self._end, self._end)

    def _parse(self, data: bytes) -> None:
        *lines, torn = data.split(b"\n")
        before = self._records[-1] if self._records else None
        for raw in lines:
            before = _parse_line(raw, before.seq + 1 if before else 1, before)
            self._records.append(before)
        self._end += len(data) - len(torn)


def _decimal(record: Record, key: str) -> Decimal:
    value = record.payload.get(key)
    if not isinstance(value, str):
        raise LedgerTamperError(record.seq, f"{record.kind} {key} must be a decimal string")
    with localcontext(_DECIMAL_CONTEXT):
        d = Decimal(value)
    if not d.is_finite():
        raise LedgerTamperError(
            record.seq, f"{record.kind} {key} {value!r} is not a finite decimal"
        )
    if d and not _DECIMAL_MIN <= d.copy_abs() <= _DECIMAL_MAX:
        raise LedgerTamperError(
            record.seq,
            f"{record.kind} {key} {value!r} must be 0 or between 1e-30 and 1e30 in size",
        )
    return d


@dataclass(frozen=True)
class Fill:
    """``size`` is unsigned and ``side`` gives the sign. A negative ``fee`` is a rebate."""

    coin: str
    side: Literal["buy", "sell"]
    size: Decimal
    price: Decimal
    fee: Decimal

    @property
    def signed_size(self) -> Decimal:
        return self.size if self.side == "buy" else -self.size

    @staticmethod
    def parse(record: Record) -> Fill:
        p = record.payload
        side = p.get("side")
        if side not in ("buy", "sell") or not isinstance(p.get("coin"), str):
            raise LedgerTamperError(record.seq, "fill needs a coin and a side of buy or sell")
        size, price = _decimal(record, "size"), _decimal(record, "price")
        if size <= 0 or price <= 0:
            raise LedgerTamperError(record.seq, "fill size and price must be above 0")
        return Fill(p["coin"], side, size, price, _decimal(record, "fee"))


def _keys_problem(record: Record, types: Mapping[str, type]) -> str | None:
    p = record.payload
    if set(p) != set(types) or any(type(p[k]) is not t for k, t in types.items()):
        shape = ", ".join(f"{k}: {t.__name__}" for k, t in types.items())
        return f"{record.kind} payload must be {{{shape}}}"
    return None


class _State:
    def __init__(self, verify: bool) -> None:
        self.verify = verify
        self.positions: dict[str, tuple[Decimal, Decimal]] = {}
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.funding = Decimal(0)
        self.latch: Record | None = None
        self.day_starts: dict[int, Decimal] = {}

    def copy(self) -> _State:
        twin = copy.copy(self)
        twin.positions, twin.day_starts = dict(self.positions), dict(self.day_starts)
        return twin

    def apply(self, record: Record) -> None:
        with localcontext(_DECIMAL_CONTEXT):
            self._apply(record)

    def _apply(self, record: Record) -> None:
        match record.kind:
            case Kind.FILL:
                fill = Fill.parse(record)
                self._fill(fill.coin, fill.signed_size, fill.price, fill.fee)
            case Kind.FUNDING:
                self.funding += _decimal(record, "amount")
            case Kind.LATCH:
                problem = _keys_problem(record, {"rule": str, "detail": str})
                if problem is not None:
                    raise LedgerTamperError(record.seq, problem)
                self.latch = record
            case Kind.UNLATCH:
                problem = _keys_problem(record, {"clears_seq": int})
                if problem is None and (
                    self.latch is None or record.payload["clears_seq"] != self.latch.seq
                ):
                    problem = "unlatch clears_seq does not name the active latch"
                if problem is not None:
                    raise LedgerTamperError(record.seq, problem)
                self.latch = None
            case Kind.DAY_START:
                self._day_start(record)
            case Kind.STATE_CHECKPOINT if self.verify:
                expected = self.snapshot()
                if dict(record.payload) != expected:
                    raise LedgerTamperError(
                        record.seq, f"checkpoint does not match replay: expected {expected}"
                    )

    def _day_start(self, record: Record) -> None:
        problem = _keys_problem(record, {"day": int, "equity_usd": str})
        if problem is not None:
            raise LedgerTamperError(record.seq, problem)
        equity = _decimal(record, "equity_usd")
        if equity <= 0:
            raise LedgerTamperError(record.seq, "day_start equity_usd must be above 0")
        day = record.payload["day"]
        if day in self.day_starts:
            raise LedgerTamperError(record.seq, f"day {day} already has a day_start")
        if day != record.ts_ms // _DAY_MS:
            raise LedgerTamperError(
                record.seq, f"day_start day {day} is not the UTC day of its ts_ms {record.ts_ms}"
            )
        self.day_starts[day] = equity

    def _fill(self, coin: str, signed: Decimal, price: Decimal, fee: Decimal) -> None:
        self.fees += fee
        self.realized -= fee
        held, avg = self.positions.get(coin, (Decimal(0), Decimal(0)))
        if held == 0 or (held > 0) == (signed > 0):
            new = held + signed
            avg = (held * avg + signed * price) / new
        else:
            closed = min(abs(held), abs(signed)) * (1 if held > 0 else -1)
            self.realized += closed * (price - avg)
            new = held + signed
            if abs(signed) > abs(held):
                avg = price
        if new == 0:
            self.positions.pop(coin, None)
        else:
            self.positions[coin] = (new, avg)

    def snapshot(self) -> dict[str, Any]:
        with localcontext(_DECIMAL_CONTEXT):
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "positions": {
                coin: {"size": str(size), "avg_price": str(avg)}
                for coin, (size, avg) in sorted(self.positions.items())
            },
            "realized_pnl": str(self.realized),
            "fees_paid": str(self.fees),
            "funding_paid": str(self.funding),
        }
