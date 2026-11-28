"""The budget guard and the cost ledger (agent design §4.7, §11.8)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import prismtex
from prismtex.agent.budget import (
    DEFAULT_LIMITS,
    LEDGER_ENV_VAR,
    BudgetExhausted,
    BudgetGuard,
    Ledger,
    LedgerLocked,
    LedgerRequired,
    Limits,
    Price,
    Reservation,
    ReserveRow,
    configured_ledger_path,
    price_micro,
    require_ledger,
)
from prismtex.agent.providers import StepUsage
from prismtex.agent.providers.catalog import spec
from prismtex.agent.providers.fake import anthropic_like, fake_spec
from prismtex.agent.types import Usage
from prismtex.settings import Settings

# USD per MTok: input 0.10, output 0.50, cache write 0.125, cache read 0.01.
HAIKU = spec("anthropic:claude-haiku-5-5")
AGENTIC = DEFAULT_LIMITS["agentic"]
SMALL = 638  # reserve(1_000, 1_000) on Haiku: ceil(1,100 x 0.125 + 1,000 x 0.5)


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def guard(ledger: Ledger | None, **options: Any) -> BudgetGuard:
    options.setdefault("max_usd_per_turn", 1.00)
    options.setdefault("cost_stop_usd", None)
    model = options.pop("spec", HAIKU)
    return BudgetGuard(options.pop("limits", AGENTIC), model, ledger, **options)


def test_default_limits_match_the_caps_table() -> None:
    assert DEFAULT_LIMITS["guided"] == Limits(
        max_steps=8, max_wall_s=180.0, max_tokens=150_000, max_tool_calls=0
    )
    assert DEFAULT_LIMITS["agentic"] == Limits(
        max_steps=24, max_wall_s=600.0, max_tokens=800_000, max_tool_calls=60
    )


@pytest.mark.anyio
async def test_guard_refuses_a_call_whose_worst_case_crosses_the_stop(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    tight = BudgetGuard(AGENTIC, HAIKU, ledger, max_usd_per_turn=1.00, cost_stop_usd=0.003)
    with pytest.raises(BudgetExhausted) as exc:
        await tight.reserve(10_000, 16_000)  # even the 4,096 floor costs 1,375 + 2,048 = 3,423
    assert exc.value.kind == "cost_total"
    assert not path.exists() or path.read_text() == ""
    roomy = BudgetGuard(AGENTIC, HAIKU, ledger, max_usd_per_turn=1.00, cost_stop_usd=0.005)
    r = await roomy.reserve(10_000, 16_000)  # output lowered to fit: (5,000 - 1,375) / 0.5
    assert r.max_output == 7_250 and r.micro == 5_000
    written = rows(path)
    assert [row["kind"] for row in written] == ["reserve"] and written[0]["micro_usd"] == 5_000


@pytest.mark.anyio
async def test_reserve_row_has_the_documented_fields(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    g = guard(Ledger(path), project_id="p1", turn_id="t_1")
    g.step()
    r = await g.reserve(1_000, 1_000)
    (row,) = rows(path)
    assert list(row) == [
        "v",
        "at",
        "kind",
        "id",
        "project_id",
        "turn_id",
        "step",
        "model",
        "micro_usd",
    ]
    assert row["v"] == 1 and row["at"].endswith("Z")
    assert (row["kind"], row["id"], row["project_id"], row["turn_id"]) == (
        "reserve",
        r.id,
        "p1",
        "t_1",
    )
    assert (row["step"], row["model"], row["micro_usd"]) == (1, HAIKU.ref, SMALL)


@pytest.mark.anyio
async def test_turn_cap_lowers_output_then_raises_cost_turn(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    g = guard(Ledger(path), max_usd_per_turn=0.005, cost_stop_usd=9.50)
    first = await g.reserve(10_000, 16_000)
    assert (first.max_output, first.micro) == (7_250, 5_000)
    assert await g.settle(first, StepUsage(10_000, 1_000)) == 1_500
    second = await g.reserve(10_000, 16_000)  # 3,500 left: (3,500 - 1,375) / 0.5
    assert (second.max_output, second.micro) == (4_250, 3_500)
    assert await g.settle(second, StepUsage(10_000, 4_250)) == 3_125
    with pytest.raises(BudgetExhausted) as exc:
        await g.reserve(10_000, 16_000)  # 375 left, the floor needs 3,423
    assert exc.value.kind == "cost_turn"
    assert [row["kind"] for row in rows(path)] == ["reserve", "settle", "reserve", "settle"]
    assert g.usage().cost_usd == 0.004625


@pytest.mark.anyio
async def test_settle_charges_reported_usage_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    g = guard(Ledger(path))
    r = await g.reserve(2_000, 1_000)
    usage = StepUsage(1000, 200, cache_read_tokens=500, cache_write_tokens=100)
    assert await g.settle(r, usage) == 218  # ceil(100 + 100 + 5 + 12.5)
    with pytest.raises(ValueError, match="settled"):
        await g.settle(r, usage)
    settle = rows(path)[-1]
    assert list(settle) == ["v", "at", "kind", "id", "micro_usd", "usage", "estimated"]
    assert (settle["kind"], settle["id"], settle["micro_usd"], settle["estimated"]) == (
        "settle",
        r.id,
        218,
        False,
    )
    assert settle["usage"] == {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
        "served_model": None,
        "iterations": [],
    }
    assert len(rows(path)) == 2
    assert g.usage() == Usage(
        steps=0,
        input_tokens=1600,
        output_tokens=200,
        cache_read_tokens=500,
        cache_write_tokens=100,
        cost_usd=0.000218,
        estimated=False,
    )


@pytest.mark.anyio
async def test_unsettled_abort_charges_the_full_reservation_marked_estimated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    g = guard(Ledger(path))
    r = await g.reserve(10_000, 16_000)
    assert r.micro == 9_375
    assert await g.settle(r, None) == 9_375
    settle = rows(path)[-1]
    assert (settle["micro_usd"], settle["usage"], settle["estimated"]) == (9_375, None, True)
    assert g.usage().estimated and g.usage().cost_usd == 0.009375
    # An aborted stream whose input count arrived: that input plus the full output reserve.
    partial = await g.reserve(10_000, 16_000)
    charged = await g.settle(partial, StepUsage(9_000, 300), estimated=True)
    assert charged == 900 + 8_000
    assert rows(path)[-1]["estimated"] is True


@pytest.mark.anyio
async def test_ledger_recover_settles_orphaned_reservations_at_full_amount(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    crashed = guard(Ledger(path))
    orphan = await crashed.reserve(10_000, 16_000)
    done = await crashed.reserve(1_000, 1_000)
    await crashed.settle(done, StepUsage(1_000, 100))  # 100 + 50
    # The process dies here: `orphan` is never settled.
    live = Ledger(path)
    assert live.committed_micro() == 9_375 + 150
    assert live.recover() == 1
    settle = rows(path)[-1]
    assert (settle["kind"], settle["id"], settle["micro_usd"]) == ("settle", orphan.id, 9_375)
    assert (settle["usage"], settle["estimated"]) == (None, True)
    assert live.committed_micro() == 9_375 + 150
    assert live.recover() == 0
    # A reservation this ledger object made and has not settled yet is not an orphan.
    in_flight = await guard(live).reserve(1_000, 1_000)
    assert live.recover() == 0
    assert live.committed_micro() == 9_375 + 150 + in_flight.micro


@pytest.mark.anyio
async def test_committed_sums_settled_plus_open_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    assert ledger.committed_micro() == 0
    g = guard(ledger)
    settled = await g.reserve(2_000, 1_000)
    await g.settle(settled, StepUsage(1000, 200, cache_read_tokens=500, cache_write_tokens=100))
    open_ = await g.reserve(10_000, 16_000)
    assert ledger.committed_micro() == 218 + open_.micro
    totals = Ledger(path).totals()
    assert (totals.settled_micro, totals.open_micro) == (218, open_.micro)
    assert totals.committed_micro == 218 + open_.micro
    assert Ledger(path).committed_micro() == 218 + open_.micro


@pytest.mark.anyio
async def test_a_torn_last_line_is_ignored_and_never_swallows_the_next_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    g = guard(Ledger(path))
    await g.reserve(1_000, 1_000)
    with path.open("ab") as out:  # a crash in the middle of a write
        out.write(b'{"v":1,"kind":"reserve","id":"r_torn","micro_us')
    reopened = Ledger(path)
    assert reopened.committed_micro() == SMALL
    await guard(reopened).reserve(1_000, 1_000)
    assert Ledger(path).committed_micro() == 2 * SMALL


@pytest.mark.anyio
async def test_concurrent_reservations_never_overshoot_the_stop(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    guards = [guard(ledger, cost_stop_usd=0.005) for _ in range(2)]
    results = await asyncio.gather(
        *(guards[i % 2].reserve(1_000, 1_000) for i in range(20)), return_exceptions=True
    )
    granted = [r for r in results if isinstance(r, Reservation)]
    refused = [r for r in results if isinstance(r, BudgetExhausted)]
    assert len(granted) == 7 and len(refused) == 13  # 7 x 638 = 4,466; an 8th would cross
    assert {r.kind for r in refused} == {"cost_total"}
    assert ledger.committed_micro() == 7 * SMALL <= 5_000


def test_reservations_from_threads_never_overshoot_the_stop(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    granted: list[int] = []

    async def reserve_many() -> int:
        g = guard(ledger, cost_stop_usd=0.02)
        count = 0
        for _ in range(25):
            try:
                await g.reserve(1_000, 1_000)
                count += 1
            except BudgetExhausted:
                pass
        return count

    threads = [
        threading.Thread(target=lambda: granted.append(asyncio.run(reserve_many())))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sum(granted) == 20_000 // SMALL  # 31
    assert ledger.committed_micro() == 31 * SMALL


_WORKER = """
import asyncio, sys, time
from pathlib import Path
from prismtex.agent.budget import DEFAULT_LIMITS, BudgetExhausted, BudgetGuard, Ledger
from prismtex.agent.providers.catalog import spec

async def main(path: str, go: str, attempts: int) -> None:
    guard = BudgetGuard(DEFAULT_LIMITS["agentic"], spec("anthropic:claude-haiku-5-5"),
                        Ledger(Path(path)), max_usd_per_turn=1.0, cost_stop_usd=0.02)
    while not Path(go).exists():
        time.sleep(0.001)
    granted = 0
    for _ in range(attempts):
        try:
            await guard.reserve(1_000, 1_000)
            granted += 1
        except BudgetExhausted:
            pass
    print(granted)

asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
"""


def test_reservations_from_two_processes_never_overshoot_the_stop(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    go = tmp_path / "go"
    src = str(Path(prismtex.__file__).parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(path), str(go), "25"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    go.touch()
    granted = 0
    for worker in workers:
        out, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, err
        granted += int(out)
    assert granted == 20_000 // SMALL  # 31 of 50 attempts
    assert Ledger(path).committed_micro() == 31 * SMALL
    assert len(rows(path)) == 31


def test_exclusive_ledger_lock_file_refuses_a_second_open(tmp_path: Path) -> None:
    path = tmp_path / "evals" / "ledger.jsonl"  # the folder is created
    first = Ledger(path, exclusive=True)
    lock = tmp_path / "evals" / "ledger.jsonl.lock"
    assert lock.exists()
    with pytest.raises(LedgerLocked) as exc:
        Ledger(path, exclusive=True)
    assert "ledger.jsonl.lock" in str(exc.value) and exc.value.path == lock
    Ledger(path).committed_micro()  # a shared open still reads and writes
    first.close()
    first.close()  # idempotent
    assert not lock.exists()
    with Ledger(path, exclusive=True):
        assert lock.exists()
    assert not lock.exists()


def test_a_closed_ledger_refuses_writes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.close()
    row = ReserveRow(id="r_1", project_id="", turn_id="", step=0, model="m", micro_usd=1)
    with pytest.raises(ValueError, match="closed"):
        ledger.append(row)


@pytest.mark.anyio
async def test_free_provider_never_writes_ledger_rows_and_step_limit_raises_steps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    g = guard(Ledger(path), spec=fake_spec, limits=DEFAULT_LIMITS["guided"], cost_stop_usd=0.0)
    r = await g.reserve(1_000, 1_000)
    assert (r.micro, r.max_output) == (0, 1_000)
    assert await g.settle(r, StepUsage(900, 100)) == 0
    await g.settle(await g.reserve(1_000, 1_000), None)
    assert not path.exists()
    for _ in range(8):
        g.step()
    with pytest.raises(BudgetExhausted) as exc:
        g.step()
    assert exc.value.kind == "steps"
    with pytest.raises(BudgetExhausted) as exc:
        g.tool_call()  # guided Turns have no tools
    assert exc.value.kind == "tool_calls"
    usage = g.usage()
    assert (usage.steps, usage.cost_usd, usage.estimated) == (8, 0.0, True)
    assert (usage.input_tokens, usage.output_tokens) == (900 + 1_100, 100 + 1_000)
    assert g.remaining().cost_usd is None


def test_tool_call_limit_raises_tool_calls() -> None:
    g = guard(None, spec=fake_spec, limits=Limits(24, 600.0, 800_000, max_tool_calls=2))
    g.tool_call()
    g.tool_call()
    with pytest.raises(BudgetExhausted) as exc:
        g.tool_call()
    assert exc.value.kind == "tool_calls"
    assert g.remaining().tool_calls == 0


@pytest.mark.anyio
async def test_token_limit_lowers_output_then_raises_tokens() -> None:
    g = guard(None, spec=fake_spec, limits=Limits(24, 600.0, max_tokens=20_000, max_tool_calls=60))
    r = await g.reserve(10_000, 16_000)  # 11,000 worst-case input leaves 9,000
    assert (r.max_output, r.micro) == (9_000, 0)
    assert g.remaining().tokens == 0
    await g.settle(r, StepUsage(10_000, 500))
    assert g.remaining().tokens == 9_500
    with pytest.raises(BudgetExhausted) as exc:
        await g.reserve(5_000, 16_000)  # 5,500 worst-case input leaves 4,000, under the floor
    assert exc.value.kind == "tokens"


@pytest.mark.anyio
async def test_wall_time_limit_raises_wall_time() -> None:
    now = [100.0]
    g = guard(None, spec=fake_spec, limits=Limits(24, 10.0, 800_000, 60), clock=lambda: now[0])
    g.step()
    now[0] = 104.0
    assert g.remaining().wall_s == 6.0
    now[0] = 110.0
    assert g.remaining().wall_s == 0.0
    for call in (g.step, g.tool_call):
        with pytest.raises(BudgetExhausted) as exc:
            call()
        assert exc.value.kind == "wall_time"
    with pytest.raises(BudgetExhausted) as exc:
        await g.reserve(10, 10)
    assert exc.value.kind == "wall_time"


@pytest.mark.anyio
async def test_paid_spec_without_a_ledger_is_refused_at_the_first_reserve() -> None:
    for model in (HAIKU, anthropic_like):
        g = guard(None, spec=model)
        with pytest.raises(BudgetExhausted) as exc:
            await g.reserve(1_000, 1_000)
        assert exc.value.kind == "cost_total"
        assert LEDGER_ENV_VAR in str(exc.value)
        assert g.usage() == Usage()


@pytest.mark.anyio
async def test_remaining_reports_steps_tokens_cost_and_time(tmp_path: Path) -> None:
    g = guard(Ledger(tmp_path / "ledger.jsonl"), max_usd_per_turn=0.01, cost_stop_usd=0.008)
    g.step()
    r = await g.reserve(1_000, 1_000)
    left = g.remaining()
    assert (left.steps, left.tool_calls) == (23, 60)
    assert left.tokens == 800_000 - 1_100 - 1_000
    assert left.cost_usd == (8_000 - SMALL) / 1_000_000  # the stop binds before the turn cap
    await g.settle(r, StepUsage(1_000, 100))
    assert g.remaining().cost_usd == (8_000 - 150) / 1_000_000
    assert 0 < left.wall_s <= 600.0


def test_price_micro_is_exact_and_rounds_up_once() -> None:
    price = Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01)
    assert price_micro(StepUsage(11_000, 0), price) == 1_100
    assert price_micro(StepUsage(1, 1), price) == 1  # 0.6 rounds up
    assert price_micro(StepUsage(0, 0, cache_read_tokens=150), price) == 2  # 1.5
    assert price_micro(StepUsage(0, 0), price) == 0


def test_price_micro_prices_each_iteration_by_its_served_model() -> None:
    main = Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20)
    fallback = Price(input=5.00, output=25.00, cache_write=6.25, cache_read=0.50)
    usage = StepUsage(
        300,
        30,
        served_model="claude-opus-5-5",
        iterations=(
            StepUsage(100, 10, served_model="claude-opus-5-5"),
            StepUsage(200, 20, served_model="claude-opus-5"),
        ),
    )
    prices = {"claude-opus-5-5": main, "claude-opus-5": fallback}
    # Only the iterations are priced: 400 + 200, then 1,000 + 500.
    assert price_micro(usage, main, prices) == 2_100
    # A served model with no listed price is charged at the given price.
    assert price_micro(StepUsage(1, 0, served_model="other"), fallback, {}) == 5


@pytest.mark.anyio
async def test_fallback_prices_make_reservations_dearer_and_settle_by_served_model(
    tmp_path: Path,
) -> None:
    opus = spec("anthropic:claude-opus-5-5")
    fallback = Price(input=5.00, output=25.00, cache_write=6.25, cache_read=0.50)
    g = guard(Ledger(tmp_path / "ledger.jsonl"), spec=opus, prices={"claude-opus-5": fallback})
    r = await g.reserve(1_000, 1_000)
    assert r.micro == 1_100 * 6.25 + 1_000 * 25  # the dearer rates of the two models
    served = StepUsage(1_000, 100, served_model="claude-opus-5-5")
    assert await g.settle(r, served) == 4_000 + 2_000
    unknown = await g.reserve(1_000, 1_000)
    assert await g.settle(unknown, StepUsage(1_000, 100)) == 5_000 + 2_500  # unnamed: dearest


def test_guard_rejects_negative_caps() -> None:
    with pytest.raises(ValueError):
        guard(None, max_usd_per_turn=-1.0)
    with pytest.raises(ValueError):
        guard(None, cost_stop_usd=-0.01)


def test_require_ledger_names_the_setting(tmp_path: Path) -> None:
    assert require_ledger(tmp_path / "ledger.jsonl") == tmp_path / "ledger.jsonl"
    with pytest.raises(LedgerRequired) as exc:
        require_ledger(None)
    assert isinstance(exc.value, ValueError)
    assert str(exc.value).startswith("a cost ledger is required for paid calls")
    assert exc.value.env_var == LEDGER_ENV_VAR == "PRISMTEX_LEDGER_PATH"
    assert LEDGER_ENV_VAR in str(exc.value)


def test_configured_ledger_path_defaults_to_the_data_state_folder(tmp_path: Path) -> None:
    default = Settings(_env_file=None, data_dir=tmp_path)  # pyright: ignore[reportCallIssue]
    assert configured_ledger_path(default) == tmp_path.resolve() / "state" / "ledger.jsonl"
    elsewhere = tmp_path / "private" / "claude-ledger.jsonl"
    chosen = Settings(_env_file=None, data_dir=tmp_path, ledger_path=elsewhere)  # pyright: ignore[reportCallIssue]
    assert configured_ledger_path(chosen) == elsewhere
