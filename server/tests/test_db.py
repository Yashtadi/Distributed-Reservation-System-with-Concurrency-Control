import os
import time
from pathlib import Path

from server.db import InMemoryDB, Wal


def make_tmp_wal(tmp_path: Path) -> Wal:
    wal_path = tmp_path / "wal.log"
    return Wal(str(wal_path))


def test_init_if_empty_creates_trains(tmp_path):
    wal = make_tmp_wal(tmp_path)
    db = InMemoryDB.from_wal(wal)
    assert db.trains == {}
    db.init_if_empty()
    assert db.trains  # non-empty
    # Should have at least one train and seat
    any_train = next(iter(db.trains.values()))
    any_coach = next(iter(any_train.coaches.values()))
    assert any_coach.seats


def test_hold_and_book_success(tmp_path):
    wal = make_tmp_wal(tmp_path)
    db = InMemoryDB.from_wal(wal)
    db.init_if_empty()

    client = "c1"
    # Legacy per-seat API still works; use one known coach
    # Pick first available coach from the initialized spec
    train = db.trains["T1"]
    coach_id = next(iter(train.coaches.keys()))
    db.hold("T1", coach_id, "1", client)
    db.book("T1", coach_id, "1", client)

    status = db.seat_status("T1", coach_id, "1")
    assert status["state"] == "booked"
    assert status["hold_by"] is None


def test_double_booking_prevented(tmp_path):
    wal = make_tmp_wal(tmp_path)
    db = InMemoryDB.from_wal(wal)
    db.init_if_empty()

    c1 = "c1"
    c2 = "c2"

    # First client holds and books
    train = db.trains["T1"]
    coach_id = next(iter(train.coaches.keys()))
    db.hold("T1", coach_id, "2", c1)
    db.book("T1", coach_id, "2", c1)

    # Second client should fail to hold the same seat
    raised = False
    try:
        db.hold("T1", coach_id, "2", c2)
    except ValueError:
        raised = True
    assert raised

    status = db.seat_status("T1", coach_id, "2")
    assert status["state"] == "booked"


def test_hold_expiry(tmp_path, monkeypatch):
    wal = make_tmp_wal(tmp_path)
    db = InMemoryDB.from_wal(wal)
    db.init_if_empty()

    client = "c1"
    t0 = time.time()

    # Freeze time for deterministic expiry
    monkeypatch.setattr("time.time", lambda: t0)
    train = db.trains["T1"]
    coach_id = next(iter(train.coaches.keys()))
    db.hold("T1", coach_id, "3", client)

    status = db.seat_status("T1", coach_id, "3")
    assert status["state"] == "held"

    # Advance time beyond TTL and trigger expiry via search
    monkeypatch.setattr("time.time", lambda: t0 + 60)
    db.search("T1")
    status_after = db.seat_status("T1", coach_id, "3")
    assert status_after["state"] == "available"


def test_wal_replay_recovers_bookings(tmp_path):
    wal_path = tmp_path / "wal.log"
    wal = Wal(str(wal_path))
    db = InMemoryDB.from_wal(wal)
    db.init_if_empty()

    client = "c1"
    train = db.trains["T1"]
    coach_id = next(iter(train.coaches.keys()))
    db.hold("T1", coach_id, "4", client)
    db.book("T1", coach_id, "4", client)

    # Simulate server restart by creating a new DB from the same WAL
    wal2 = Wal(str(wal_path))
    db2 = InMemoryDB.from_wal(wal2)

    status = db2.seat_status("T1", coach_id, "4")
    assert status["state"] == "booked"


def test_tier_booking_and_waitlist(tmp_path):
    wal = make_tmp_wal(tmp_path)
    db = InMemoryDB.from_wal(wal)
    db.init_if_empty()

    train_id = "T1"
    tier = "1AC"

    # 1AC has 4 seats per train in the demo spec
    clients = [f"c{i}" for i in range(1, 7)]
    tickets = []
    for c in clients:
        info = db.book_by_tier(train_id, tier, c)
        tickets.append(info)

    # First 4 should be confirmed, last 2 should be waiting
    confirmed = [t for t in tickets if t["ticket_status"] == "confirmed"]
    waiting = [t for t in tickets if t["ticket_status"] == "waiting"]
    assert len(confirmed) == 4
    assert len(waiting) == 2

    # Cancel one confirmed ticket and ensure the first waiting is promoted
    first_confirmed_ticket = confirmed[0]["ticket"]["ticket_id"]
    waiting_ticket_id = waiting[0]["ticket"]["ticket_id"]

    client_id = confirmed[0]["ticket"]["client_id"]
    result = db.cancel_ticket(first_confirmed_ticket, client_id)
    assert result["cancelled_ticket"]["status"] == "cancelled"
    promoted = result.get("promoted_ticket")
    assert promoted is not None
    assert promoted["ticket_id"] == waiting_ticket_id
    assert promoted["status"] == "confirmed"

