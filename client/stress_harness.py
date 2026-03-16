import argparse
import asyncio
import random
import time
from typing import List

from .client import Client


async def worker(client_id: str, host: str, port: int, rounds: int) -> int:
    c = Client(host, port, client_id)
    await c.connect()
    success = 0
    try:
        for _ in range(rounds):
            train = random.choice(["T1", "T2"])
            tier = random.choice(["1AC", "2AC", "3AC"])
            resp = await c.book_tier(train, tier)
            if resp.get("status") != "OK":
                continue
            ticket = resp.get("data", {}).get("ticket") or {}
            ticket_status = resp.get("data", {}).get("ticket_status")
            ticket_id = ticket.get("ticket_id")
            if ticket_status == "confirmed":
                success += 1
                # Occasionally cancel to exercise promotion
                if random.random() < 0.2 and ticket_id:
                    await c.cancel_ticket(ticket_id)
            else:
                # Sometimes cancel waiting tickets as well
                if random.random() < 0.1 and ticket_id:
                    await c.cancel_ticket(ticket_id)
    finally:
        await c.close()
    return success


async def main_async(host: str, port: int, n_clients: int, rounds: int) -> None:
    t0 = time.time()
    tasks: List[asyncio.Task[int]] = []
    for i in range(n_clients):
        tasks.append(asyncio.create_task(worker(f"c{i}", host, port, rounds)))
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - t0
    total_bookings = sum(results)
    print(f"Total successful bookings: {total_bookings}")
    print(f"Elapsed: {elapsed:.3f}s, throughput: {total_bookings/elapsed:.2f} bookings/s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stress test for train reservation server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(main_async(args.host, args.port, args.clients, args.rounds))


if __name__ == "__main__":
    main()

