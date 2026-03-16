import argparse
import asyncio
import time
import uuid
from typing import Any, Dict

from .db import InMemoryDB, Wal
from .lock_manager import LockManager
from .protocol import ProtocolError, read_message, write_message
from .replication import BackupReplicator, PrimaryReplicator


class ReservationServer:
    def __init__(
        self,
        host: str,
        port: int,
        wal_path: str,
        *,
        role: str = "primary",
        repl_host: str | None = None,
        repl_port: int = 9001,
        primary_repl_host: str = "127.0.0.1",
        primary_repl_port: int = 9001,
        hb_interval_s: float = 1.0,
        hb_timeout_s: float = 3.5,
    ) -> None:
        self._host = host
        self._port = port
        self._wal = Wal(wal_path)
        self._db = InMemoryDB.from_wal(self._wal)
        self._db.init_if_empty()
        self._locks = LockManager()

        self._role = role
        self._repl_host = repl_host or host
        self._repl_port = repl_port
        self._primary_repl_host = primary_repl_host
        self._primary_repl_port = primary_repl_port
        self._hb_interval_s = hb_interval_s
        self._hb_timeout_s = hb_timeout_s

        self._client_server: asyncio.AbstractServer | None = None
        self._primary_repl: PrimaryReplicator | None = None
        self._promoted = asyncio.Event()

    async def start(self) -> None:
        if self._role not in ("primary", "backup"):
            raise ValueError("role must be 'primary' or 'backup'")

        if self._role == "primary":
            await self._start_primary()
            await self._serve_clients_forever()
            return

        # Backup: start replication client and wait until promotion triggers
        await self._start_backup_until_promotion()
        # Now promoted to primary
        await self._start_primary()
        await self._serve_clients_forever()

    async def _start_primary(self) -> None:
        # Start client listener
        self._client_server = await asyncio.start_server(self._handle_client, self._host, self._port)
        addrs = ", ".join(str(sock.getsockname()) for sock in self._client_server.sockets or [])
        print(f"[server:{self._role}] Serving clients on {addrs}")

        # Start replication server
        self._primary_repl = PrimaryReplicator(
            host=self._repl_host,
            port=self._repl_port,
            wal=self._wal,
            hb_interval_s=self._hb_interval_s,
        )
        await self._primary_repl.start()

    async def _serve_clients_forever(self) -> None:
        if not self._client_server:
            raise RuntimeError("client server not started")
        async with self._client_server:
            await self._client_server.serve_forever()

    async def _start_backup_until_promotion(self) -> None:
        def _on_promote() -> None:
            self._promoted.set()

        backup = BackupReplicator(
            primary_host=self._primary_repl_host,
            primary_port=self._primary_repl_port,
            wal=self._wal,
            db=self._db,
            hb_timeout_s=self._hb_timeout_s,
        )
        task = asyncio.create_task(backup.run(on_promote=_on_promote))
        await self._promoted.wait()
        await backup.stop()
        try:
            await task
        except Exception:  # noqa: BLE001
            pass
        self._role = "primary"

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        print(f"[server] Client connected: {peer}")
        try:
            while True:
                try:
                    msg = await read_message(reader)
                except asyncio.IncompleteReadError:
                    break
                except ProtocolError as e:
                    print(f"[server] protocol error from {peer}: {e}")
                    break

                resp = await self._handle_request(msg)
                await write_message(writer, resp)
        finally:
            print(f"[server] Client disconnected: {peer}")
            writer.close()
            await writer.wait_closed()

    async def _handle_request(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        cmd = msg.get("cmd")
        req_id = msg.get("req_id") or str(uuid.uuid4())
        client_id = msg.get("client_id", "unknown")
        payload = msg.get("payload") or {}
        ts = time.time()

        try:
            if cmd == "SEARCH":
                train = payload["train"]
                data = self._db.search(train)
                return self._ok(req_id, data, ts)
            elif cmd == "HOLD":
                train = payload["train"]
                coach = payload["coach"]
                seat = payload["seat"]
                acquired = await self._locks.acquire(f"{train}-{coach}-{seat}", timeout=5.0)
                if not acquired:
                    return self._error(req_id, "LOCK_TIMEOUT", "seat busy", ts)
                try:
                    info = self._db.hold(train, coach, str(seat), client_id)
                except ValueError as e:
                    self._locks.release(f"{train}-{coach}-{seat}")
                    return self._error(req_id, "HOLD_FAILED", str(e), ts)
                return self._ok(req_id, info, ts)
            elif cmd == "BOOK":
                # Tier-based automatic allocation if 'tier' is provided
                if "tier" in payload:
                    train = payload["train"]
                    tier = payload["tier"]
                    key = f"{train}-TIER-{tier}"
                    acquired = await self._locks.acquire(key, timeout=5.0)
                    if not acquired:
                        return self._error(req_id, "LOCK_TIMEOUT", "tier busy", ts)
                    try:
                        info = self._db.book_by_tier(train, tier, client_id)
                    except ValueError as e:
                        self._locks.release(key)
                        return self._error(req_id, "BOOK_FAILED", str(e), ts)
                    self._locks.release(key)
                    return self._ok(req_id, info, ts)

                # Legacy per-seat booking path (kept for compatibility)
                train = payload["train"]
                coach = payload["coach"]
                seat = payload["seat"]
                key = f"{train}-{coach}-{seat}"
                acquired = await self._locks.acquire(key, timeout=5.0)
                if not acquired:
                    return self._error(req_id, "LOCK_TIMEOUT", "seat busy", ts)
                try:
                    self._db.book(train, coach, str(seat), client_id)
                except ValueError as e:
                    self._locks.release(key)
                    return self._error(req_id, "BOOK_FAILED", str(e), ts)
                self._locks.release(key)
                return self._ok(req_id, {"status": "booked"}, ts)
            elif cmd == "RELEASE":
                train = payload["train"]
                coach = payload["coach"]
                seat = payload["seat"]
                key = f"{train}-{coach}-{seat}"
                acquired = await self._locks.acquire(key, timeout=5.0)
                if not acquired:
                    return self._error(req_id, "LOCK_TIMEOUT", "seat busy", ts)
                try:
                    self._db.release(train, coach, str(seat), client_id)
                finally:
                    self._locks.release(key)
                return self._ok(req_id, {"status": "released"}, ts)
            elif cmd == "STATUS":
                # Ticket-oriented status if ticket_id is provided
                if "ticket_id" in payload:
                    ticket_id = payload["ticket_id"]
                    data = self._db.ticket_status(ticket_id)
                    if data is None:
                        return self._error(req_id, "NOT_FOUND", "ticket not found", ts)
                    return self._ok(req_id, data, ts)
                # Legacy seat-based status
                train = payload["train"]
                coach = payload["coach"]
                seat = payload["seat"]
                data = self._db.seat_status(train, coach, str(seat))
                return self._ok(req_id, data, ts)
            elif cmd == "CANCEL":
                ticket_id = payload["ticket_id"]
                # Look up ticket metadata to determine tier lock
                meta = self._db.get_ticket_metadata(ticket_id)
                if meta is None:
                    return self._error(req_id, "NOT_FOUND", "ticket not found", ts)
                train, tier = meta
                key = f"{train}-TIER-{tier}"
                acquired = await self._locks.acquire(key, timeout=5.0)
                if not acquired:
                    return self._error(req_id, "LOCK_TIMEOUT", "tier busy", ts)
                try:
                    info = self._db.cancel_ticket(ticket_id, client_id)
                except ValueError as e:
                    self._locks.release(key)
                    return self._error(req_id, "CANCEL_FAILED", str(e), ts)
                self._locks.release(key)
                return self._ok(req_id, info, ts)
            elif cmd == "HEARTBEAT":
                return self._ok(req_id, {"status": "alive"}, ts)
            else:
                return self._error(req_id, "UNKNOWN_CMD", f"Unknown command {cmd}", ts)
        except Exception as e:  # noqa: BLE001
            return self._error(req_id, "SERVER_ERROR", str(e), ts)

    @staticmethod
    def _ok(req_id: str, data: Dict[str, Any], ts: float) -> Dict[str, Any]:
        return {"req_id": req_id, "status": "OK", "code": "OK", "data": data, "ts": ts}

    @staticmethod
    def _error(req_id: str, code: str, msg: str, ts: float) -> Dict[str, Any]:
        return {"req_id": req_id, "status": "ERROR", "code": code, "message": msg, "ts": ts}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Train reservation server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--wal", default="data/wal.log")
    parser.add_argument("--role", choices=["primary", "backup"], default="primary")
    parser.add_argument("--repl-host", default=None, help="Replication bind host (primary)")
    parser.add_argument("--repl-port", type=int, default=9001, help="Replication port (primary)")
    parser.add_argument("--primary-repl-host", default="127.0.0.1", help="Primary replication host (backup)")
    parser.add_argument("--primary-repl-port", type=int, default=9001, help="Primary replication port (backup)")
    parser.add_argument("--hb-interval", type=float, default=1.0, help="Heartbeat interval seconds (primary)")
    parser.add_argument("--hb-timeout", type=float, default=3.5, help="Heartbeat timeout seconds (backup)")
    args = parser.parse_args()

    server = ReservationServer(
        args.host,
        args.port,
        args.wal,
        role=args.role,
        repl_host=args.repl_host,
        repl_port=args.repl_port,
        primary_repl_host=args.primary_repl_host,
        primary_repl_port=args.primary_repl_port,
        hb_interval_s=args.hb_interval,
        hb_timeout_s=args.hb_timeout,
    )
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())

