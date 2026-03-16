import argparse
import asyncio
import time
import uuid
from typing import Any, Dict

from server.protocol import read_message, write_message


class Client:
    def __init__(self, host: str, port: int, client_id: str) -> None:
        self._host = host
        self._port = port
        self.client_id = client_id
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

    async def _send(self, cmd: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._writer or not self._reader:
            raise RuntimeError("client not connected")
        req = {
            "cmd": cmd,
            "req_id": str(uuid.uuid4()),
            "client_id": self.client_id,
            "payload": payload,
            "ts": time.time(),
        }
        await write_message(self._writer, req)
        resp = await read_message(self._reader)
        return resp

    async def search(self, train: str) -> Dict[str, Any]:
        return await self._send("SEARCH", {"train": train})

    async def book_tier(self, train: str, tier: str) -> Dict[str, Any]:
        return await self._send("BOOK", {"train": train, "tier": tier})

    async def cancel_ticket(self, ticket_id: str) -> Dict[str, Any]:
        return await self._send("CANCEL", {"ticket_id": ticket_id})

    async def ticket_status(self, ticket_id: str) -> Dict[str, Any]:
        return await self._send("STATUS", {"ticket_id": ticket_id})


async def interactive_client(host: str, port: int, client_id: str) -> None:
    c = Client(host, port, client_id)
    await c.connect()
    print(f"Connected as {client_id} to {host}:{port}")
    try:
        while True:
            cmd = input(
                "Enter command (SEARCH/BOOK/CANCEL/STATUS/quit): "
            ).strip().upper()
            if cmd in ("QUIT", "EXIT"):
                break
            try:
                if cmd == "SEARCH":
                    train = input("Train ID: ").strip()
                    resp = await c.search(train)
                elif cmd == "BOOK":
                    train = input("Train ID: ").strip()
                    tier = input("Tier (1AC/2AC/3AC): ").strip().upper()
                    resp = await c.book_tier(train, tier)
                elif cmd == "CANCEL":
                    ticket_id = input("Ticket ID: ").strip()
                    resp = await c.cancel_ticket(ticket_id)
                elif cmd == "STATUS":
                    ticket_id = input("Ticket ID: ").strip()
                    resp = await c.ticket_status(ticket_id)
                else:
                    print("Unknown command")
                    continue
                print("Response:", resp)
            except Exception as e:  # noqa: BLE001
                print("Error:", e)
    finally:
        await c.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Train reservation client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--client-id", default="c1")
    args = parser.parse_args()
    asyncio.run(interactive_client(args.host, args.port, args.client_id))


if __name__ == "__main__":
    main()

