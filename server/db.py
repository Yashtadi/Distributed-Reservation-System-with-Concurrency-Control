from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class Seat:
    seat_id: str
    tier: str
    state: str = "available"  # available | held | booked
    hold_by: Optional[str] = None
    hold_expires: Optional[float] = None


@dataclass
class Coach:
    coach_id: str
    seats: Dict[str, Seat]


@dataclass
class Train:
    train_id: str
    coaches: Dict[str, Coach]


@dataclass
class Ticket:
    ticket_id: str
    client_id: str
    train_id: str
    tier: str
    seat_id: Optional[str]
    status: str  # confirmed | waiting | cancelled
    created_at: float


class Wal:
    """
    Very simple append-only JSON line WAL.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self._path, "a+", buffering=1, encoding="utf-8")
        self._subscribers: list[callable] = []

    def subscribe(self, callback) -> None:
        """
        Register a callback invoked after successful append(entry).
        Callback signature: callback(entry: dict) -> None
        """
        self._subscribers.append(callback)

    def append(self, entry: dict) -> None:
        line = json.dumps(entry, separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        for cb in list(self._subscribers):
            cb(entry)

    def replay(self) -> List[dict]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class InMemoryDB:
    """
    In-memory data model with simple WAL-backed recovery.
    """

    def __init__(self, wal: Wal, hold_ttl: float = 30.0) -> None:
        self._wal = wal
        self._hold_ttl = hold_ttl
        self.trains: Dict[str, Train] = {}
        self.tickets: Dict[str, Ticket] = {}
        # key: (train_id, tier) -> FIFO list of ticket_ids
        self.waiting_lists: Dict[Tuple[str, str], List[str]] = {}

    @classmethod
    def from_wal(cls, wal: Wal, hold_ttl: float = 30.0) -> "InMemoryDB":
        db = cls(wal=wal, hold_ttl=hold_ttl)
        for entry in wal.replay():
            db._apply_entry(entry, from_replay=True)
        return db

    def _apply_entry(self, entry: dict, from_replay: bool = False) -> None:
        op = entry.get("op")
        if op == "INIT":
            self._init_trains(entry["trains"])
        elif op == "HOLD":
            self._hold_no_log(
                entry["train"],
                entry["coach"],
                entry["seat"],
                entry["client"],
                entry["ts"],
            )
        elif op == "BOOK":
            self._book_no_log(
                entry["train"],
                entry["coach"],
                entry["seat"],
                entry["client"],
                entry["ts"],
            )
        elif op == "RELEASE":
            self._release_no_log(
                entry["train"],
                entry["coach"],
                entry["seat"],
                entry["client"],
            )
        elif op == "BOOK_TIER":
            self._book_tier_no_log(
                ticket_id=entry["ticket_id"],
                client_id=entry["client"],
                train_id=entry["train"],
                tier=entry["tier"],
                seat_id=entry["seat_id"],
                status=entry["status"],
                ts=entry["ts"],
            )
        elif op == "WAITLIST_ADD":
            self._waitlist_add_no_log(
                ticket_id=entry["ticket_id"],
                client_id=entry["client"],
                train_id=entry["train"],
                tier=entry["tier"],
                ts=entry["ts"],
            )
        elif op == "CANCEL_TICKET":
            self._cancel_ticket_no_log(
                ticket_id=entry["ticket_id"],
                train_id=entry["train"],
                tier=entry["tier"],
                seat_id=entry.get("seat_id"),
            )
        elif op == "PROMOTE_TICKET":
            self._promote_ticket_no_log(
                ticket_id=entry["ticket_id"],
                train_id=entry["train"],
                tier=entry["tier"],
                seat_id=entry["seat_id"],
            )

    def apply_wal_entry(self, entry: dict) -> None:
        """
        Apply a WAL entry to the in-memory state without writing a new WAL entry.
        Intended for backups applying replicated operations.
        """
        self._apply_entry(entry, from_replay=True)

    def _init_trains(self, trains_def: List[dict]) -> None:
        self.trains.clear()
        self.tickets.clear()
        self.waiting_lists.clear()
        for t in trains_def:
            coaches: Dict[str, Coach] = {}
            for c in t["coaches"]:
                seats: Dict[str, Seat] = {}
                tier = c.get("tier", "3AC")
                for s in c["seats"]:
                    seat_id = f"{t['train_id']}-{c['coach_id']}-{s}"
                    seats[seat_id] = Seat(seat_id=seat_id, tier=tier)
                coaches[c["coach_id"]] = Coach(coach_id=c["coach_id"], seats=seats)
            self.trains[t["train_id"]] = Train(train_id=t["train_id"], coaches=coaches)

    def init_if_empty(self) -> None:
        if self.trains:
            return
        # Basic demo configuration with tiers:
        # - T1 and T2
        # - 1AC: 4 seats
        # - 2AC: 8 seats
        # - 3AC: 8 seats
        spec = [
            {
                "train_id": "T1",
                "coaches": [
                    {"coach_id": "1A", "tier": "1AC", "seats": list(range(1, 5))},
                    {"coach_id": "2A", "tier": "2AC", "seats": list(range(1, 9))},
                    {"coach_id": "3A", "tier": "3AC", "seats": list(range(1, 9))},
                ],
            },
            {
                "train_id": "T2",
                "coaches": [
                    {"coach_id": "1B", "tier": "1AC", "seats": list(range(1, 5))},
                    {"coach_id": "2B", "tier": "2AC", "seats": list(range(1, 9))},
                    {"coach_id": "3B", "tier": "3AC", "seats": list(range(1, 9))},
                ],
            },
        ]
        self._init_trains(spec)
        self._wal.append({"op": "INIT", "trains": spec, "ts": time.time()})

    def search(self, train_id: str) -> dict:
        train = self.trains.get(train_id)
        if not train:
            return {"train": train_id, "tiers": []}
        now = time.time()
        self._expire_holds(now)
        # Aggregate by tier
        tier_available: Dict[str, int] = {}
        for coach in train.coaches.values():
            for seat in coach.seats.values():
                if seat.state == "available":
                    tier_available[seat.tier] = tier_available.get(seat.tier, 0) + 1
        tiers = []
        for tier, available in tier_available.items():
            wait_len = len(self.waiting_lists.get((train_id, tier), []))
            tiers.append(
                {
                    "tier": tier,
                    "available_seats": available,
                    "waiting_list_length": wait_len,
                }
            )
        # Ensure all known tiers for the train are represented, even if 0 available
        known_tiers = {
            seat.tier
            for coach in train.coaches.values()
            for seat in coach.seats.values()
        }
        for tier in sorted(known_tiers):
            if not any(t["tier"] == tier for t in tiers):
                tiers.append(
                    {
                        "tier": tier,
                        "available_seats": 0,
                        "waiting_list_length": len(
                            self.waiting_lists.get((train_id, tier), [])
                        ),
                    }
                )
        return {"train": train_id, "tiers": sorted(tiers, key=lambda t: t["tier"])}

    def _get_seat(self, train_id: str, coach_id: str, seat_num: str) -> Tuple[Train, Coach, Seat]:
        train = self.trains[train_id]
        coach = train.coaches[coach_id]
        seat_id = f"{train_id}-{coach_id}-{seat_num}"
        seat = coach.seats[seat_id]
        return train, coach, seat

    def hold(self, train_id: str, coach_id: str, seat_num: str, client_id: str) -> dict:
        now = time.time()
        self._expire_holds(now)
        self._hold_no_log(train_id, coach_id, seat_num, client_id, now)
        self._wal.append(
            {
                "op": "HOLD",
                "train": train_id,
                "coach": coach_id,
                "seat": seat_num,
                "client": client_id,
                "ts": now,
            }
        )
        return {"hold_expires_at": now + self._hold_ttl}

    def _hold_no_log(self, train_id: str, coach_id: str, seat_num: str, client_id: str, ts: float) -> None:
        _, _, seat = self._get_seat(train_id, coach_id, seat_num)
        if seat.state != "available":
            raise ValueError("seat not available")
        seat.state = "held"
        seat.hold_by = client_id
        seat.hold_expires = ts + self._hold_ttl

    def book(self, train_id: str, coach_id: str, seat_num: str, client_id: str) -> None:
        now = time.time()
        self._expire_holds(now)
        self._book_no_log(train_id, coach_id, seat_num, client_id, now)
        self._wal.append(
            {
                "op": "BOOK",
                "train": train_id,
                "coach": coach_id,
                "seat": seat_num,
                "client": client_id,
                "ts": now,
            }
        )

    def _book_no_log(self, train_id: str, coach_id: str, seat_num: str, client_id: str, ts: float) -> None:
        _, _, seat = self._get_seat(train_id, coach_id, seat_num)
        if seat.state != "held" or seat.hold_by != client_id or (seat.hold_expires and seat.hold_expires < ts):
            raise ValueError("cannot book seat")
        seat.state = "booked"
        seat.hold_by = None
        seat.hold_expires = None

    def release(self, train_id: str, coach_id: str, seat_num: str, client_id: str) -> None:
        self._release_no_log(train_id, coach_id, seat_num, client_id)
        self._wal.append(
            {
                "op": "RELEASE",
                "train": train_id,
                "coach": coach_id,
                "seat": seat_num,
                "client": client_id,
                "ts": time.time(),
            }
        )

    def _release_no_log(self, train_id: str, coach_id: str, seat_num: str, client_id: str) -> None:
        _, _, seat = self._get_seat(train_id, coach_id, seat_num)
        if seat.state == "held" and seat.hold_by == client_id:
            seat.state = "available"
            seat.hold_by = None
            seat.hold_expires = None

    def _expire_holds(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        for train in self.trains.values():
            for coach in train.coaches.values():
                for seat in coach.seats.values():
                    if seat.state == "held" and seat.hold_expires and seat.hold_expires < now:
                        seat.state = "available"
                        seat.hold_by = None
                        seat.hold_expires = None

    def seat_status(self, train_id: str, coach_id: str, seat_num: str) -> dict:
        _, _, seat = self._get_seat(train_id, coach_id, seat_num)
        return asdict(seat)

    def coach_available_seats(self, train_id: str, coach_id: str) -> dict:
        train = self.trains.get(train_id)
        if train is None:
            raise ValueError("train not found")
        coach = train.coaches.get(coach_id)
        if coach is None:
            raise ValueError("coach not found")

        self._expire_holds()
        available_numbers: list[int] = []
        for seat in coach.seats.values():
            if seat.state != "available":
                continue
            # seat_id format: <train>-<coach>-<seat_number>
            seat_num = int(seat.seat_id.rsplit("-", 1)[1])
            available_numbers.append(seat_num)
        available_numbers.sort()
        return {
            "train": train_id,
            "coach": coach_id,
            "available_seat_numbers": available_numbers,
            "available_count": len(available_numbers),
        }

    def tier_available_seat_numbers(self, train_id: str, tier: str) -> dict:
        train = self.trains.get(train_id)
        if train is None:
            raise ValueError("train not found")

        self._expire_holds()
        available_numbers: set[int] = set()
        for coach in train.coaches.values():
            for seat in coach.seats.values():
                if seat.tier != tier or seat.state != "available":
                    continue
                seat_num = int(seat.seat_id.rsplit("-", 1)[1])
                available_numbers.add(seat_num)

        numbers = sorted(available_numbers)
        return {
            "train": train_id,
            "tier": tier,
            "available_seat_numbers": numbers,
            "available_count": len(numbers),
        }

    # --- Tier-based booking and waiting list management ---

    def _iter_seats_for_tier(self, train_id: str, tier: str):
        train = self.trains[train_id]
        for coach in train.coaches.values():
            for seat in coach.seats.values():
                if seat.tier == tier:
                    yield seat

    def _book_tier_no_log(
        self,
        ticket_id: str,
        client_id: str,
        train_id: str,
        tier: str,
        seat_id: Optional[str],
        status: str,
        ts: float,
    ) -> None:
        ticket = Ticket(
            ticket_id=ticket_id,
            client_id=client_id,
            train_id=train_id,
            tier=tier,
            seat_id=seat_id,
            status=status,
            created_at=ts,
        )
        self.tickets[ticket_id] = ticket
        if seat_id is not None:
            # Confirmed ticket; mark seat as booked
            train = self.trains[train_id]
            # seat_id encodes coach and seat number; find matching seat
            for coach in train.coaches.values():
                if seat_id in coach.seats:
                    seat = coach.seats[seat_id]
                    seat.state = "booked"
                    seat.hold_by = None
                    seat.hold_expires = None
                    break

    def _waitlist_add_no_log(
        self,
        ticket_id: str,
        client_id: str,
        train_id: str,
        tier: str,
        ts: float,
    ) -> None:
        ticket = Ticket(
            ticket_id=ticket_id,
            client_id=client_id,
            train_id=train_id,
            tier=tier,
            seat_id=None,
            status="waiting",
            created_at=ts,
        )
        self.tickets[ticket_id] = ticket
        key = (train_id, tier)
        self.waiting_lists.setdefault(key, []).append(ticket_id)

    def _cancel_ticket_no_log(
        self,
        ticket_id: str,
        train_id: str,
        tier: str,
        seat_id: Optional[str],
    ) -> None:
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            return
        ticket.status = "cancelled"
        # If it was in waiting list, remove it
        key = (train_id, tier)
        if ticket_id in self.waiting_lists.get(key, []):
            self.waiting_lists[key] = [
                t for t in self.waiting_lists[key] if t != ticket_id
            ]
        # If confirmed, free the seat
        if seat_id:
            train = self.trains[train_id]
            for coach in train.coaches.values():
                if seat_id in coach.seats:
                    seat = coach.seats[seat_id]
                    seat.state = "available"
                    seat.hold_by = None
                    seat.hold_expires = None
                    break

    def _promote_ticket_no_log(
        self,
        ticket_id: str,
        train_id: str,
        tier: str,
        seat_id: str,
    ) -> None:
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            return
        ticket.status = "confirmed"
        ticket.seat_id = seat_id
        key = (train_id, tier)
        if ticket_id in self.waiting_lists.get(key, []):
            self.waiting_lists[key] = [
                t for t in self.waiting_lists[key] if t != ticket_id
            ]
        train = self.trains[train_id]
        for coach in train.coaches.values():
            if seat_id in coach.seats:
                seat = coach.seats[seat_id]
                seat.state = "booked"
                seat.hold_by = None
                seat.hold_expires = None
                break

    def book_by_tier(self, train_id: str, tier: str, client_id: str) -> dict:
        """
        Automatically allocate a seat within the requested tier if available.
        Otherwise, enqueue the request into the tier-specific waiting list.
        """
        now = time.time()
        self._expire_holds(now)

        # Find first available seat in this tier
        chosen_seat: Optional[Seat] = None
        for seat in self._iter_seats_for_tier(train_id, tier):
            if seat.state == "available":
                chosen_seat = seat
                break

        ticket_id = f"TKT-{int(now * 1000)}-{len(self.tickets) + 1}"

        if chosen_seat is not None:
            # Confirmed immediately
            self._book_tier_no_log(
                ticket_id=ticket_id,
                client_id=client_id,
                train_id=train_id,
                tier=tier,
                seat_id=chosen_seat.seat_id,
                status="confirmed",
                ts=now,
            )
            self._wal.append(
                {
                    "op": "BOOK_TIER",
                    "ticket_id": ticket_id,
                    "client": client_id,
                    "train": train_id,
                    "tier": tier,
                    "seat_id": chosen_seat.seat_id,
                    "status": "confirmed",
                    "ts": now,
                }
            )
            return {
                "ticket": asdict(self.tickets[ticket_id]),
                "ticket_status": "confirmed",
            }

        # No seat available: enqueue in waiting list
        self._waitlist_add_no_log(
            ticket_id=ticket_id,
            client_id=client_id,
            train_id=train_id,
            tier=tier,
            ts=now,
        )
        self._wal.append(
            {
                "op": "WAITLIST_ADD",
                "ticket_id": ticket_id,
                "client": client_id,
                "train": train_id,
                "tier": tier,
                "ts": now,
            }
        )
        key = (train_id, tier)
        position = self.waiting_lists[key].index(ticket_id) + 1
        return {
            "ticket": asdict(self.tickets[ticket_id]),
            "ticket_status": "waiting",
            "waiting_position": position,
        }

    def book_by_tier_and_seat_number(
        self, train_id: str, tier: str, seat_number: int, client_id: str
    ) -> dict:
        """
        Book a specific seat number within a tier.
        """
        now = time.time()
        self._expire_holds(now)
        seat_suffix = f"-{seat_number}"

        chosen_seat: Optional[Seat] = None
        for seat in self._iter_seats_for_tier(train_id, tier):
            if seat.state != "available":
                continue
            if seat.seat_id.endswith(seat_suffix):
                chosen_seat = seat
                break

        if chosen_seat is None:
            raise ValueError("requested seat number not available in this tier")

        ticket_id = f"TKT-{int(now * 1000)}-{len(self.tickets) + 1}"
        self._book_tier_no_log(
            ticket_id=ticket_id,
            client_id=client_id,
            train_id=train_id,
            tier=tier,
            seat_id=chosen_seat.seat_id,
            status="confirmed",
            ts=now,
        )
        self._wal.append(
            {
                "op": "BOOK_TIER",
                "ticket_id": ticket_id,
                "client": client_id,
                "train": train_id,
                "tier": tier,
                "seat_id": chosen_seat.seat_id,
                "status": "confirmed",
                "ts": now,
            }
        )
        return {
            "ticket": asdict(self.tickets[ticket_id]),
            "ticket_status": "confirmed",
        }

    def get_ticket_metadata(self, ticket_id: str) -> Optional[Tuple[str, str]]:
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            return None
        return ticket.train_id, ticket.tier

    def ticket_status(self, ticket_id: str) -> Optional[dict]:
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            return None
        return asdict(ticket)

    def cancel_ticket(self, ticket_id: str, client_id: str) -> dict:
        """
        Cancel a ticket. If confirmed, free the seat and promote from waiting list.
        If waiting, remove from waiting list.
        """
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            raise ValueError("ticket not found")
        if ticket.client_id != client_id:
            raise ValueError("ticket does not belong to this client")

        train_id = ticket.train_id
        tier = ticket.tier
        seat_id = ticket.seat_id

        # Cancel the ticket
        self._cancel_ticket_no_log(
            ticket_id=ticket_id,
            train_id=train_id,
            tier=tier,
            seat_id=seat_id,
        )
        self._wal.append(
            {
                "op": "CANCEL_TICKET",
                "ticket_id": ticket_id,
                "client": client_id,
                "train": train_id,
                "tier": tier,
                "seat_id": seat_id,
                "ts": time.time(),
            }
        )

        promoted: Optional[Ticket] = None
        # If a seat was freed, try to promote from waiting list
        if seat_id is not None:
            key = (train_id, tier)
            queue = self.waiting_lists.get(key, [])
            if queue:
                next_ticket_id = queue[0]
                self._promote_ticket_no_log(
                    ticket_id=next_ticket_id,
                    train_id=train_id,
                    tier=tier,
                    seat_id=seat_id,
                )
                promoted = self.tickets[next_ticket_id]
                self._wal.append(
                    {
                        "op": "PROMOTE_TICKET",
                        "ticket_id": next_ticket_id,
                        "client": promoted.client_id,
                        "train": train_id,
                        "tier": tier,
                        "seat_id": seat_id,
                        "ts": time.time(),
                    }
                )

        result = {
            "cancelled_ticket": asdict(self.tickets[ticket_id]),
        }
        if promoted is not None:
            result["promoted_ticket"] = asdict(promoted)
        return result

