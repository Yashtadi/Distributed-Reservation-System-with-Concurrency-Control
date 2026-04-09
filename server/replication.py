import asyncio
import time
from typing import Any, Dict, Optional, Set

from .db import InMemoryDB, Wal
from .protocol import read_message, write_message


class PrimaryReplicator:
    """
    Primary-side replication server:
    - Accepts backup connections
    - Streams WAL entries to all backups
    - Sends periodic heartbeats
    """

    def __init__(self, host: str, port: int, wal: Wal, hb_interval_s: float = 1.0) -> None:
        self._host = host
        self._port = port
        self._wal = wal
        self._hb_interval_s = hb_interval_s

        self._server: Optional[asyncio.AbstractServer] = None
        self._writers: Set[asyncio.StreamWriter] = set()
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

        # Subscribe to WAL appends so we can broadcast them
        loop = asyncio.get_event_loop()

        def _on_wal(entry: dict) -> None:
            loop.call_soon_threadsafe(self._queue.put_nowait, entry)

        self._wal.subscribe(_on_wal)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_backup, self._host, self._port)
        self._tasks.append(asyncio.create_task(self._broadcast_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        print(f"[repl-primary] Serving replication on {addrs}")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        for w in list(self._writers):
            try:
                w.close()
                await w.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._writers.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_backup(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._writers.add(writer)
        print(f"[repl-primary] Backup connected: {peer}")
        try:
            # Basic handshake: backup sends HELLO, we ACK.
            msg = await read_message(reader)
            if msg.get("type") != "HELLO":
                await write_message(writer, {"type": "ERROR", "message": "expected HELLO"})
                return
            await write_message(writer, {"type": "ACK", "role": "primary", "ts": time.time()})
            # Keep connection open; primary does not expect inbound messages currently.
            while True:
                await asyncio.sleep(3600)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            print(f"[repl-primary] Backup disconnected: {peer}")

    async def _broadcast_loop(self) -> None:
        while True:
            entry = await self._queue.get()
            msg = {"type": "WAL", "entry": entry}
            dead: list[asyncio.StreamWriter] = []
            for w in self._writers:
                try:
                    await write_message(w, msg)
                except Exception:  # noqa: BLE001
                    dead.append(w)
            for w in dead:
                self._writers.discard(w)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._hb_interval_s)
            msg = {"type": "HB", "ts": time.time()}
            dead: list[asyncio.StreamWriter] = []
            for w in self._writers:
                try:
                    await write_message(w, msg)
                except Exception:  # noqa: BLE001
                    dead.append(w)
            for w in dead:
                self._writers.discard(w)


class BackupReplicator:
    """
    Backup-side replication client:
    - Connects to the primary replicator
    - Receives WAL entries + heartbeats
    - Persists entries locally and applies to DB
    - Triggers promotion callback if heartbeats stop
    """

    def __init__(
        self,
        primary_host: str,
        primary_port: int,
        wal: Wal,
        db: InMemoryDB,
        hb_timeout_s: float = 3.5,
    ) -> None:
        self._primary_host = primary_host
        self._primary_port = primary_port
        self._wal = wal
        self._db = db
        self._hb_timeout_s = hb_timeout_s

        self._last_hb: float = 0.0
        self._stop = asyncio.Event()

    async def run(self, *, on_promote) -> None:
        """
        Runs until promotion or explicit stop.
        """
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(self._primary_host, self._primary_port)
                await write_message(writer, {"type": "HELLO", "ts": time.time()})
                ack = await read_message(reader)
                if ack.get("type") != "ACK":
                    raise RuntimeError(f"bad ack: {ack}")
                print(f"[repl-backup] Connected to primary at {self._primary_host}:{self._primary_port}")
                self._last_hb = time.time()

                monitor = asyncio.create_task(self._monitor_heartbeat(on_promote))
                try:
                    while True:
                        msg = await read_message(reader)
                        t = msg.get("type")
                        if t == "HB":
                            self._last_hb = time.time()
                        elif t == "WAL":
                            entry = msg["entry"]
                            # Persist then apply
                            self._wal.append(entry)
                            self._db.apply_wal_entry(entry)
                        else:
                            # Unknown replication message types are ignored in this skeleton
                            pass
                finally:
                    monitor.cancel()
                    writer.close()
                    await writer.wait_closed()
            except PromotionTriggered:
                return
            except Exception as e:  # noqa: BLE001
                print(f"[repl-backup] replication error: {e}; retrying...")
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self._stop.set()

    async def _monitor_heartbeat(self, on_promote) -> None:
        while True:
            await asyncio.sleep(0.25)
            if time.time() - self._last_hb > self._hb_timeout_s:
                print("[repl-backup] heartbeat timeout; promoting to primary")
                on_promote()
                raise PromotionTriggered()


class PromotionTriggered(Exception):
    pass

