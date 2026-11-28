from __future__ import annotations

import asyncio

import pytest

from prismtex.cancel import Cancelled, CancelToken


@pytest.mark.anyio
async def test_cancel_token_set_is_seen_by_raise_if_set_and_wait() -> None:
    token = CancelToken()
    assert not token.is_set()
    token.raise_if_set()

    waiter = asyncio.ensure_future(token.wait())
    await asyncio.sleep(0)
    assert not waiter.done()

    token.set()
    assert token.is_set()
    with pytest.raises(Cancelled):
        token.raise_if_set()
    await asyncio.wait_for(waiter, 1)


@pytest.mark.anyio
async def test_wait_returns_at_once_when_already_set() -> None:
    token = CancelToken()
    token.set()
    token.set()  # idempotent
    await asyncio.wait_for(token.wait(), 1)


def test_a_token_can_be_set_outside_an_event_loop() -> None:
    token = CancelToken()
    token.set()
    assert token.is_set()
