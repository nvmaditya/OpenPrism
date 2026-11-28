"""Turn budgets, the cost guard and the cost ledger (agent design §2.2 `budget`, §4.7, §11.8).

Amounts are integer micro-USD. A price in USD per million tokens is micro-USD per token;
the arithmetic uses exact fractions and rounds up once per reservation or settlement.

`BudgetGuard` meters one Turn: Steps, tool calls, wall time, tokens and, for paid models,
cost. Before a paid Step it reserves the Step's worst-case cost in the `Ledger`, lowering
the Step's output cap to fit the per-Turn cap and the cumulative stop; after the Step it
settles the reservation at the reported usage.

The `Ledger` is one JSONL file shared by every process that makes paid calls, the app and
eval runs alike. Each row is one line, fsynced before the call returns. Writers take an
OS file lock on `<path>.mutex` (kept beside the ledger, never removed), re-read the rows
other writers appended, and only then decide and append, so settled cost plus open
reservations never crosses a stop across threads and processes. `exclusive=True` also
creates `<path>.lock` for the life of the object, so a second exclusive open (a second
eval run) raises `LedgerLocked`.
"""

from __future__ import annotations

import errno
import json
import math
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, Self

from prismtex.agent.types import ProfileName, Usage
from prismtex.layout import DataLayout

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

if TYPE_CHECKING:
    # Type-only: the providers package imports this module for `Price`.
    from prismtex.agent.providers import ModelSpec, StepUsage
    from prismtex.settings import Settings

__all__ = [
    "DEFAULT_LIMITS",
    "LEDGER_ENV_VAR",
    "BudgetExhausted",
    "BudgetGuard",
    "BudgetKind",
    "Ledger",
    "LedgerLocked",
    "LedgerRequired",
    "LedgerRow",
    "LedgerTotals",
    "Limits",
    "Price",
    "Remaining",
    "Reservation",
    "ReserveRow",
    "SettleRow",
    "configured_ledger_path",
    "price_micro",
    "require_ledger",
]

LEDGER_ENV_VAR: Final = "PRISMTEX_LEDGER_PATH"

_MICRO: Final = 1_000_000
_OUTPUT_FLOOR: Final = 4_096  # a lowered output cap never goes below min(this, max_output)
_LOCK_TIMEOUT_S: Final = 10.0
_LEDGER_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Limits:
    """The caps of one Turn (agent design §4.7)."""

    max_steps: int
    max_wall_s: float
    max_tokens: int
    max_tool_calls: int


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens; `cache_write` is the 5-minute cache write rate."""

    input: float
    output: float
    cache_write: float
    cache_read: float


DEFAULT_LIMITS: Mapping[ProfileName, Limits] = MappingProxyType(
    {
        "guided": Limits(max_steps=8, max_wall_s=180.0, max_tokens=150_000, max_tool_calls=0),
        "agentic": Limits(max_steps=24, max_wall_s=600.0, max_tokens=800_000, max_tool_calls=60),
    }
)

BudgetKind = Literal["steps", "wall_time", "tokens", "tool_calls", "cost_turn", "cost_total"]

_EXHAUSTED_MESSAGES: Mapping[str, str] = {
    "steps": "The turn used all of its model calls.",
    "wall_time": "The turn ran out of time.",
    "tokens": "The next model call could cross the turn's token limit.",
    "tool_calls": "The turn used all of its tool calls.",
    "cost_turn": "The next model call could cross the turn's cost cap.",
    "cost_total": "The next model call could cross the cumulative cost stop.",
}


class BudgetExhausted(Exception):
    """A limit of the Turn would be crossed; `kind` names it (agent design §9.3)."""

    kind: BudgetKind
    message: str

    def __init__(self, kind: BudgetKind, message: str | None = None) -> None:
        if kind not in _EXHAUSTED_MESSAGES:
            raise ValueError(f"unknown budget kind: {kind!r}")
        self.kind = kind
        self.message = message or _EXHAUSTED_MESSAGES[kind]
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class Reservation:
    """A reserved Step: `micro` is its worst-case cost; `max_output` the output cap to send."""

    id: str
    micro: int
    max_output: int


@dataclass(frozen=True, slots=True)
class Remaining:
    """What is left of the Turn's limits.

    `tokens` and `cost_usd` count open reservations as spent. `cost_usd` is None for a
    free model; for a paid one it is the smaller of what is left under the per-Turn cap
    and under the cumulative stop.
    """

    steps: int
    tokens: int
    cost_usd: float | None
    wall_s: float
    tool_calls: int


# -- prices --------------------------------------------------------------------


def _rate(value: float) -> Fraction:
    # str() first, so 0.10 is exactly 1/10 and not its binary approximation.
    return Fraction(str(value))


def _micro_of(usd: float) -> int:
    return math.floor(_rate(usd) * _MICRO)


def _usd(micro: int) -> str:
    if micro % 10_000 == 0:
        return f"${micro / _MICRO:,.2f}"
    return f"${micro / _MICRO:.6f}".rstrip("0")


def _cost(usage: StepUsage, default: Price, prices: Mapping[str, Price]) -> Fraction:
    if usage.iterations:
        return sum((_cost(part, default, prices) for part in usage.iterations), Fraction(0))
    price = default if usage.served_model is None else prices.get(usage.served_model, default)
    return (
        usage.input_tokens * _rate(price.input)
        + usage.output_tokens * _rate(price.output)
        + usage.cache_read_tokens * _rate(price.cache_read)
        + usage.cache_write_tokens * _rate(price.cache_write)
    )


def price_micro(usage: StepUsage, price: Price, prices: Mapping[str, Price] | None = None) -> int:
    """The cost of `usage` in micro-USD, rounded up once.

    When `usage.iterations` is non-empty, its entries are the parts of the Step: each is
    priced by its served model, `prices[served_model]`, and the top-level counts are not
    priced again. A part with no served model, or one `prices` does not list, is priced
    at `price`.
    """
    return math.ceil(_cost(usage, price, prices or {}))


def _counts(usage: StepUsage) -> tuple[int, int, int, int]:
    """Uncached input, output, cache-read and cache-write tokens, by the rule of `price_micro`."""
    if not usage.iterations:
        return (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
    parts = [_counts(part) for part in usage.iterations]
    return (
        sum(p[0] for p in parts),
        sum(p[1] for p in parts),
        sum(p[2] for p in parts),
        sum(p[3] for p in parts),
    )


def _dearest(prices: Iterable[Price]) -> Price:
    rows = list(prices)
    return Price(
        input=max(p.input for p in rows),
        output=max(p.output for p in rows),
        cache_write=max(p.cache_write for p in rows),
        cache_read=max(p.cache_read for p in rows),
    )


def _fit(room: int, input_cost: Fraction, output_rate: Fraction, cap: int) -> int:
    """The largest output cap, at most `cap`, whose worst case fits `room`; -1 if none does."""
    spare = room - input_cost
    if spare < 0:
        return -1
    if output_rate == 0:
        return cap
    return min(cap, math.floor(spare / output_rate))


# -- ledger rows ---------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReserveRow:
    """A reservation, appended and fsynced before its request is sent."""

    kind: ClassVar[Literal["reserve"]] = "reserve"
    id: str
    project_id: str
    turn_id: str
    step: int
    model: str
    micro_usd: int
    at: str = field(default_factory=_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": _LEDGER_VERSION,
            "at": self.at,
            "kind": self.kind,
            "id": self.id,
            "project_id": self.project_id,
            "turn_id": self.turn_id,
            "step": self.step,
            "model": self.model,
            "micro_usd": self.micro_usd,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SettleRow:
    """The settlement of a reservation; `usage` is None when nothing was reported."""

    kind: ClassVar[Literal["settle"]] = "settle"
    id: str
    micro_usd: int
    usage: StepUsage | None
    estimated: bool
    at: str = field(default_factory=_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": _LEDGER_VERSION,
            "at": self.at,
            "kind": self.kind,
            "id": self.id,
            "micro_usd": self.micro_usd,
            "usage": None if self.usage is None else asdict(self.usage),
            "estimated": self.estimated,
        }


LedgerRow = ReserveRow | SettleRow


def _encode(row: LedgerRow) -> bytes:
    return (json.dumps(row.to_json(), separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, slots=True)
class LedgerTotals:
    """Sums over a ledger file, in micro-USD."""

    settled_micro: int
    open_micro: int

    @property
    def committed_micro(self) -> int:
        return self.settled_micro + self.open_micro


# -- configuration -------------------------------------------------------------


class LedgerLocked(Exception):
    """Another run holds the ledger; `path` names the lock file."""

    path: Path

    def __init__(self, path: Path, message: str | None = None) -> None:
        self.path = path
        super().__init__(
            message
            or f"The cost ledger is in use by another run: {path} exists. "
            "If no other run is active, delete that file."
        )


class LedgerRequired(ValueError):
    """A paid model was set up without a cost ledger; `env_var` names the setting."""

    env_var: str

    def __init__(self) -> None:
        self.env_var = LEDGER_ENV_VAR
        super().__init__(f"a cost ledger is required for paid calls; set {LEDGER_ENV_VAR}")


def require_ledger(ledger_path: Path | None) -> Path:
    """`ledger_path` itself; raises `LedgerRequired` when it is None.

    Code that would make a paid call without a `BudgetGuard` in front of it (a paid
    adapter's constructor) calls this first, so no paid call can run unmetered.
    """
    if ledger_path is None:
        raise LedgerRequired()
    return ledger_path


def configured_ledger_path(settings: Settings) -> Path:
    """`Settings.ledger_path` (`PRISMTEX_LEDGER_PATH`), else `<data_dir>/state/ledger.jsonl`."""
    if settings.ledger_path is not None:
        return settings.ledger_path
    return DataLayout(settings.data_dir).ledger


# -- the OS file lock ----------------------------------------------------------

_BUSY = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK})
_O_BINARY: int = getattr(os, "O_BINARY", 0)

if sys.platform == "win32":

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _BUSY:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _BUSY:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _os_lock(path: Path, timeout_s: float) -> Iterator[None]:
    """Hold an exclusive OS lock on `path`; the OS drops it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        delay = 0.001
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LedgerLocked(
                    path, f"Timed out after {timeout_s:g} s waiting for the ledger lock {path}."
                )
            time.sleep(delay)
            delay = min(delay * 2, 0.02)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


# -- the ledger ----------------------------------------------------------------


class Ledger:
    """The persistent cost ledger: one JSON object per line, appended and fsynced.

    `committed_micro()` is settled rows plus open reservations, re-read from the file
    on every call, so it includes rows other processes appended. A settle row counts
    once per id (the first one); a line that is not a valid row, such as a torn last
    line after a crash, is skipped. Safe to share between threads.
    """

    def __init__(self, path: Path, *, exclusive: bool = False) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.mutex_path = self.path.with_name(self.path.name + ".mutex")
        self.exclusive = exclusive
        self._lock = threading.RLock()
        self._closed = False
        self._offset = 0  # bytes of the file already read, always at a line end
        self._torn = False  # the file ends in a partial line
        self._reserved: dict[str, int] = {}
        self._settled: dict[str, int] = {}
        self._settled_micro = 0
        self._open_micro = 0
        self._in_flight: set[str] = set()  # reserved through this object, not yet settled
        self._lock_token = ""
        if exclusive:
            self._create_lock_file()

    def committed_micro(self) -> int:
        """Settled rows plus open reservations, in micro-USD."""
        return self.totals().committed_micro

    def totals(self) -> LedgerTotals:
        """Settled rows and open reservations, in micro-USD, as the file holds them now."""
        with self._lock:
            self._refresh()
            return LedgerTotals(settled_micro=self._settled_micro, open_micro=self._open_micro)

    def append(self, row: LedgerRow) -> None:
        """Append one row as one JSON line, then fsync; raises ValueError once closed."""
        with self._hold():
            self._write((row,))

    def recover(self) -> int:
        """Settle every orphaned reservation at its full amount, marked estimated.

        An orphan is an open reservation that this object did not make, such as one
        left by a crashed process. Returns how many were settled. The committed total
        does not change: open reservations already count at their full amount. Call it
        at startup: a reservation another live process has in flight would be settled
        early, and its own settlement then ignored, which can only overcount.
        """
        with self._hold():
            orphans = [
                SettleRow(id=rid, micro_usd=micro, usage=None, estimated=True)
                for rid, micro in self._reserved.items()
                if rid not in self._settled and rid not in self._in_flight
            ]
            if orphans:
                self._write(orphans)
            return len(orphans)

    def close(self) -> None:
        """Refuse further writes and, for an exclusive ledger, remove the lock file."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.exclusive:
                self._remove_lock_file()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals: callers in this module hold `_hold()` for `_committed` and `_write`.

    @contextmanager
    def _hold(self) -> Iterator[None]:
        """The object's lock, then the OS lock on `<path>.mutex`, with the rows re-read."""
        with self._lock:
            if self._closed:
                raise ValueError("the cost ledger is closed")
            with _os_lock(self.mutex_path, _LOCK_TIMEOUT_S):
                self._refresh()
                yield

    def _committed(self) -> int:
        return self._settled_micro + self._open_micro

    def _write(self, rows: Sequence[LedgerRow]) -> None:
        data = b"".join(_encode(row) for row in rows)
        if self._torn:
            data = b"\n" + data  # end the partial line, so it cannot swallow this row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        for row in rows:
            if isinstance(row, ReserveRow):
                self._in_flight.add(row.id)
            else:
                self._in_flight.discard(row.id)
        self._refresh()

    def _refresh(self) -> None:
        try:
            source = self.path.open("rb")
        except FileNotFoundError:
            self._reset()
            return
        with source:
            if os.fstat(source.fileno()).st_size < self._offset:
                self._reset()  # the file was cut or replaced: read it again
            source.seek(self._offset)
            data = source.read()
        end = data.rfind(b"\n") + 1  # a partial last line waits for its newline
        for line in data[:end].split(b"\n"):
            self._apply(line)
        self._offset += end
        self._torn = end < len(data)

    def _reset(self) -> None:
        self._offset = 0
        self._torn = False
        self._reserved.clear()
        self._settled.clear()
        self._settled_micro = 0
        self._open_micro = 0

    def _apply(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            row = json.loads(line)
        except ValueError:
            return
        if not isinstance(row, dict):
            return
        rid, micro, kind = row.get("id"), row.get("micro_usd"), row.get("kind")
        if not isinstance(rid, str) or isinstance(micro, bool) or not isinstance(micro, int):
            return
        if micro < 0:
            return
        if kind == "reserve" and rid not in self._reserved:
            self._reserved[rid] = micro
            if rid not in self._settled:
                self._open_micro += micro
        elif kind == "settle" and rid not in self._settled:
            self._settled[rid] = micro
            self._settled_micro += micro
            if rid in self._reserved:
                self._open_micro -= self._reserved[rid]

    def _create_lock_file(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o644)
        except FileExistsError:
            raise LedgerLocked(self.lock_path) from None
        token = f"pid {os.getpid()} at {_now()} ({uuid.uuid4().hex})\n"
        try:
            try:
                os.write(fd, token.encode())
            finally:
                os.close(fd)
        except OSError:
            self.lock_path.unlink(missing_ok=True)
            raise
        self._lock_token = token

    def _remove_lock_file(self) -> None:
        try:
            if self.lock_path.read_text(encoding="utf-8") == self._lock_token:
                self.lock_path.unlink()
        except FileNotFoundError:
            pass  # someone removed it already; a newer run's lock file is left alone


# -- the guard -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Open:
    reservation: Reservation
    input_tokens: int  # the worst-case input reserved

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.reservation.max_output


class BudgetGuard:
    """The limits of one Turn and, for a paid model, its reservations in the ledger.

    - `step()` and `tool_call()` count a model call and a tool call; `reserve` and
      `settle` bracket each model call. Every limit raises `BudgetExhausted` with its kind.
    - `reserve` checks the token limit, then for a paid model the per-Turn cap and the
      cumulative stop. Each may lower `max_output` to fit, down to a floor of
      `min(4096, max_output)`; if even the floor does not fit, nothing is written.
    - A free model (`spec.paid` False) never writes ledger rows.
    - A paid model with no ledger or no price is refused at the first `reserve`
      (`cost_total`).
    - `prices` lists other models that may serve this spec's requests (server-side
      fallbacks), by model id. Reservations use the dearest rate of all of them; a
      settlement prices each part by its served model, and an unlisted one at the
      dearest rate.

    A guard belongs to one Turn and one event loop; its ledger may be shared. `reserve`
    and `settle` do their ledger I/O inline and never await, so a cancellation cannot
    fall between a row being written and the guard recording it.
    """

    def __init__(
        self,
        limits: Limits,
        spec: ModelSpec,
        ledger: Ledger | None,
        *,
        max_usd_per_turn: float,
        cost_stop_usd: float | None,
        project_id: str = "",
        turn_id: str = "",
        prices: Mapping[str, Price] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_usd_per_turn < 0:
            raise ValueError("max_usd_per_turn must not be negative")
        if cost_stop_usd is not None and cost_stop_usd < 0:
            raise ValueError("cost_stop_usd must not be negative")
        self._limits = limits
        self._spec = spec
        self._ledger = ledger
        self._turn_cap = _micro_of(max_usd_per_turn)
        self._stop = None if cost_stop_usd is None else _micro_of(cost_stop_usd)
        self._project_id = project_id
        self._turn_id = turn_id
        others = dict(prices or {})
        self._prices: dict[str, Price] = others
        self._dearest: Price | None = None
        if spec.price is not None:
            self._prices = {**others, spec.model: spec.price}
            self._dearest = _dearest([spec.price, *others.values()])
        self._clock = clock
        self._started = clock()
        self._steps = 0
        self._tool_calls = 0
        self._open: dict[str, _Open] = {}
        self._tokens = 0  # settled input plus output tokens
        self._spent = 0  # settled micro-USD
        self._input = 0
        self._output = 0
        self._cache_read = 0
        self._cache_write = 0
        self._estimated = False

    def step(self) -> None:
        """Count one model call; raises `BudgetExhausted("steps" | "wall_time")`."""
        self._check_time()
        if self._steps >= self._limits.max_steps:
            raise BudgetExhausted(
                "steps", f"The turn reached its limit of {self._limits.max_steps} model calls."
            )
        self._steps += 1

    def tool_call(self) -> None:
        """Count one tool call; raises `BudgetExhausted("tool_calls" | "wall_time")`."""
        self._check_time()
        if self._tool_calls >= self._limits.max_tool_calls:
            raise BudgetExhausted(
                "tool_calls",
                f"The turn reached its limit of {self._limits.max_tool_calls} tool calls.",
            )
        self._tool_calls += 1

    async def reserve(self, est_input: int, max_output: int) -> Reservation:
        """Reserve one Step before it is sent; the Step must send `max_output` as returned.

        The worst case is `ceil(est_input × 1.1)` input tokens at the dearer of the input
        and cache-write rates, plus `max_output` output tokens. For a paid model the
        reserve row is appended and fsynced before this returns. Raises
        `BudgetExhausted` ("wall_time", "tokens", "cost_turn" or "cost_total").
        """
        if est_input < 0 or max_output < 1:
            raise ValueError("est_input must be >= 0 and max_output >= 1")
        self._check_time()
        floor = min(_OUTPUT_FLOOR, max_output)
        worst_input = -(-est_input * 11 // 10)
        token_room = self._limits.max_tokens - self._committed_tokens() - worst_input
        if token_room < floor:
            raise BudgetExhausted(
                "tokens",
                f"The next model call could cross the turn's limit of "
                f"{self._limits.max_tokens:,} tokens.",
            )
        allowed = min(max_output, token_room)
        if not self._spec.paid:
            return self._opened(Reservation(_new_id(), 0, allowed), worst_input)

        ledger, price = self._ledger, self._dearest
        if ledger is None:
            raise BudgetExhausted(
                "cost_total",
                f"A paid model needs a cost ledger: set {LEDGER_ENV_VAR}. Nothing was sent.",
            )
        if price is None:
            raise BudgetExhausted(
                "cost_total", "This paid model has no known price, so it cannot be metered."
            )
        input_cost = worst_input * max(_rate(price.input), _rate(price.cache_write))
        output_rate = _rate(price.output)
        turn_room = self._turn_cap - self._spent - self._open_micro()
        allowed = _fit(turn_room, input_cost, output_rate, allowed)
        if allowed < floor:
            raise BudgetExhausted(
                "cost_turn",
                f"The next model call could cross the turn's cost cap of {_usd(self._turn_cap)}.",
            )
        with ledger._hold():
            if self._stop is not None:
                allowed = _fit(self._stop - ledger._committed(), input_cost, output_rate, allowed)
                if allowed < floor:
                    raise BudgetExhausted(
                        "cost_total",
                        f"The next model call could cross the cumulative cost stop of "
                        f"{_usd(self._stop)}.",
                    )
            reservation = Reservation(
                _new_id(), math.ceil(input_cost + allowed * output_rate), allowed
            )
            ledger._write(
                (
                    ReserveRow(
                        id=reservation.id,
                        project_id=self._project_id,
                        turn_id=self._turn_id,
                        step=self._steps,
                        model=self._spec.ref,
                        micro_usd=reservation.micro,
                    ),
                )
            )
        return self._opened(reservation, worst_input)

    async def settle(
        self, r: Reservation, usage: StepUsage | None, *, estimated: bool = False
    ) -> int:
        """Settle `r` exactly once and return the micro-USD charged.

        - With reported `usage`: its cost, each part priced by its served model.
        - `usage` None: the full reservation, marked estimated.
        - `estimated=True` with `usage` (an aborted stream whose input count arrived):
          that input plus `r.max_output` output tokens.
        A second settlement of the same reservation raises ValueError.
        """
        held = self._open.get(r.id)
        if held is None or held.reservation != r:
            raise ValueError(f"reservation {r.id} is not open: unknown or already settled")
        if usage is None:
            estimated = True
            counts = (held.input_tokens, r.max_output, 0, 0)
            micro = r.micro if self._spec.paid else 0
        else:
            charged = usage
            if estimated:
                output = max(usage.output_tokens, r.max_output)
                charged = replace(usage, output_tokens=output, iterations=())
            counts = _counts(charged)
            micro = 0
            if self._spec.paid and self._dearest is not None:
                micro = price_micro(charged, self._dearest, self._prices)
        if self._spec.paid and self._ledger is not None:
            self._ledger.append(
                SettleRow(id=r.id, micro_usd=micro, usage=usage, estimated=estimated)
            )
        del self._open[r.id]
        uncached, output_tokens, cache_read, cache_write = counts
        prompt = uncached + cache_read + cache_write
        self._input += prompt
        self._output += output_tokens
        self._cache_read += cache_read
        self._cache_write += cache_write
        self._tokens += prompt + output_tokens
        self._spent += micro
        self._estimated = self._estimated or estimated
        return micro

    def usage(self) -> Usage:
        """What the Turn consumed so far; `input_tokens` counts cached prompt tokens too."""
        return Usage(
            steps=self._steps,
            input_tokens=self._input,
            output_tokens=self._output,
            cache_read_tokens=self._cache_read,
            cache_write_tokens=self._cache_write,
            cost_usd=self._spent / _MICRO,
            estimated=self._estimated,
        )

    def remaining(self) -> Remaining:
        """What is left of each limit; reads the ledger for the cumulative stop."""
        limits = self._limits
        cost: float | None = None
        if self._spec.paid:
            room = 0
            if self._ledger is not None and self._dearest is not None:
                room = self._turn_cap - self._spent - self._open_micro()
                if self._stop is not None:
                    room = min(room, self._stop - self._ledger.committed_micro())
            cost = max(room, 0) / _MICRO
        return Remaining(
            steps=max(0, limits.max_steps - self._steps),
            tokens=max(0, limits.max_tokens - self._committed_tokens()),
            cost_usd=cost,
            wall_s=max(0.0, limits.max_wall_s - (self._clock() - self._started)),
            tool_calls=max(0, limits.max_tool_calls - self._tool_calls),
        )

    def _check_time(self) -> None:
        if self._clock() - self._started >= self._limits.max_wall_s:
            raise BudgetExhausted(
                "wall_time", f"The turn reached its time limit of {self._limits.max_wall_s:g} s."
            )

    def _committed_tokens(self) -> int:
        return self._tokens + sum(held.tokens for held in self._open.values())

    def _open_micro(self) -> int:
        return sum(held.reservation.micro for held in self._open.values())

    def _opened(self, reservation: Reservation, input_tokens: int) -> Reservation:
        self._open[reservation.id] = _Open(reservation, input_tokens)
        return reservation


def _new_id() -> str:
    return f"r_{uuid.uuid4().hex}"
