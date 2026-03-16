import asyncio

import pytest

from server.lock_manager import LockManager


@pytest.mark.asyncio
async def test_single_key_lock_excludes_others():
    lm = LockManager()
    key = "seat-1"

    order = []

    async def worker(name: str):
        acquired = await lm.acquire(key, timeout=1.0)
        assert acquired
        order.append(f"{name}-acquired")
        # hold the lock briefly
        await asyncio.sleep(0.1)
        lm.release(key)
        order.append(f"{name}-released")

    await asyncio.gather(worker("w1"), worker("w2"))

    # Exactly one worker should acquire first; the other only after release.
    assert order[0] == "w1-acquired" or order[0] == "w2-acquired"
    assert order[1].endswith("released")
    assert order[2].endswith("acquired")
    assert order[3].endswith("released")


@pytest.mark.asyncio
async def test_timeout_when_lock_held():
    lm = LockManager()
    key = "seat-2"

    async def holder():
        acquired = await lm.acquire(key)
        assert acquired
        await asyncio.sleep(0.3)
        lm.release(key)

    async def waiter():
        # Short timeout, should fail while holder keeps lock
        acquired = await lm.acquire(key, timeout=0.05)
        assert acquired is False

    await asyncio.gather(holder(), waiter())

