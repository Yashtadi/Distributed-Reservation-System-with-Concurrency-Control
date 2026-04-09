import asyncio
from typing import Dict


class LockManager:
    """
    Simple per-key asyncio lock manager.
    Provides fine-grained locking on seat IDs or similar resources.
    """

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def acquire(self, key: str, timeout: float | None = None) -> bool:
        async with self._global_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock

        if timeout is None:
            await lock.acquire()
            return True

        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self, key: str) -> None:
        lock = self._locks.get(key)
        if lock and lock.locked():
            lock.release()
