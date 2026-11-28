"""Cancellation for a Turn: a flag that synchronous code polls and async code awaits."""

from __future__ import annotations

import asyncio

__all__ = ["CancelToken", "Cancelled"]


class Cancelled(Exception):
    """The Turn was cancelled; raised by `CancelToken.raise_if_set`."""


class CancelToken:
    """Set once, never cleared.

    `is_set` and `raise_if_set` work anywhere. `wait` must run on the event
    loop the token is first awaited on; `set` is called on that loop too (the
    cancel route and the Turn share it), since asyncio.Event is not
    thread-safe.
    """

    def __init__(self) -> None:
        self._set = False
        self._event = asyncio.Event()

    def set(self) -> None:
        self._set = True
        self._event.set()

    def is_set(self) -> bool:
        return self._set

    def raise_if_set(self) -> None:
        if self._set:
            raise Cancelled()

    async def wait(self) -> None:
        await self._event.wait()
