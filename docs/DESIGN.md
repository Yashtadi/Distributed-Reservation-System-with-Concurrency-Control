## Design Overview

This document describes the Phase 1 design of the distributed train reservation system: data model, concurrency control, durability, and server architecture.

### Data model

- **Hierarchy**: Trains → Coaches → Seats.
- **Tiers**:
  - Each seat belongs to exactly one tier: `1AC`, `2AC`, or `3AC`.
  - Coaches are an internal grouping; the tier is attached to each `Seat`.
- **Seat state**:
  - `available`: free to be allocated.
  - `held`: reserved temporarily by a client (used by legacy per-seat API), with an expiry timestamp.
  - `booked`: permanently reserved by a confirmed ticket.
- **Tickets**:
  - Each booking request creates a `Ticket` with:
    - `ticket_id`
    - `client_id`
    - `train_id`
    - `tier`
    - `seat_id` (for confirmed tickets; `null` while waiting)
    - `status`: `confirmed`, `waiting`, or `cancelled`
- **Tier-specific waiting lists**:
  - For each `(train_id, tier)` pair, the DB maintains a FIFO queue of ticket IDs representing the waiting list.

Identifiers:

- Train: string ID, e.g. `T1`.
- Tier: `1AC`, `2AC`, `3AC`.
- Internal `seat_id` remains `"{train_id}-{coach_id}-{seat_num}"`, but is not exposed to clients anymore in Phase 1.

The in-memory model is implemented in `server/db.py` using `Seat`, `Coach`, `Train`, and `Ticket` dataclasses and an `InMemoryDB` wrapper that owns all trains, tickets, and waiting lists.

### Concurrency control, tiers, and waiting lists

Phase 1 uses **locking-based concurrency control** with:

- **Fine-grained per-seat locks** for the legacy per-seat API (`HOLD` / `BOOK` / `RELEASE`).
- **Per-tier locks** for the new tier-based booking and waiting-list logic.

Locking is implemented with `LockManager` (`server/lock_manager.py`):

- Seat-level locks: key `"{train}-{coach}-{seat}"`, used by:
  - `HOLD`
  - `BOOK` (legacy mode)
  - `RELEASE`
- Tier-level locks: key `"{train}-TIER-{tier}"`, used by:
  - Tier-based `BOOK` (automatic seat allocation)
  - `CANCEL` (ticket cancellation and promotion)

Inside each critical section, the server calls into `InMemoryDB` to:

- For tier-based booking:
  - Find the first `available` seat within the requested tier.
  - If found, mark it `booked` and create a `Ticket` with `status="confirmed"`.
  - If not found, create a `Ticket` with `status="waiting"` and append it to the tier’s FIFO waiting list.
  - Append a WAL entry describing the tier booking or waitlist addition.
- For tier-based cancellation:
  - Mark the ticket as `cancelled`.
  - If it was `confirmed`, free its seat.
  - Then, if the waiting list for that tier is non-empty, promote the **front ticket**:
    - Remove it from the waiting list.
    - Assign the freed seat.
    - Update its status to `confirmed`.
    - Persist the promotion in the WAL.

**Data consistency and double-booking prevention**:

- Only one request at a time may hold the lock for a given `(train, tier)`, so **automatic seat allocation and waiting-list promotion are atomic**.
- Seat-level state is still validated by `InMemoryDB` when transitioning states, and the same underlying `Seat` objects are shared across tier logic.
- Because all operations that can change seat assignments or waiting lists for a tier are serialized under the same lock, there is no chance for:
  - Two tickets to be assigned the same seat.
  - Lost promotions or duplicate entries in the waiting list.

### Durability and recovery (WAL)

The system uses a simple **write-ahead log (WAL)** to persist operations:

- WAL implementation: `Wal` in `server/db.py`.
- Format: **JSON Lines**:
  - Each line is a compact JSON object with at least `op` and relevant fields.
- Durability:
  - Every `append(entry)`:
    - Serializes the entry.
    - Writes it to the log.
    - Flushes and calls `os.fsync` to ensure it is on disk before returning.
- Operations logged:
  - `INIT`: initial train/coach/seat layout.
  - `HOLD`: successful hold acquisition.
  - `BOOK`: successful booking.
  - `RELEASE`: release of a hold.

On startup:

- The server creates a `Wal` object pointing at the configured log file.
- It constructs `InMemoryDB.from_wal(wal)`, which:
  - Reads every WAL entry.
  - Re-applies them in order via `_apply_entry`.
- After replay, `init_if_empty()` seeds demo data only if no trains exist (i.e., empty WAL).

This ensures that `booked` seats survive crashes and that the in-memory state is rebuilt consistently from disk.

### Replication and failover skeleton (Phase 1)

Phase 1 includes a **primary/backup replication skeleton** implemented over a separate TCP channel.

- **Primary replication server** (`server/replication.py`, `PrimaryReplicator`)
  - Listens on a replication port (default `9001`).
  - Broadcasts each newly appended WAL entry to all connected backups.
  - Sends periodic heartbeats so backups can detect primary failure.
- **Backup replication client** (`server/replication.py`, `BackupReplicator`)
  - Connects to the primary’s replication port.
  - Receives:
    - `WAL` messages (persists them locally and applies them to its in-memory DB).
    - `HB` messages (heartbeats).
  - If heartbeats stop for more than a configured timeout, it **promotes itself** to primary.

**Scope note (Phase 1):**

- Promotion uses a simplified “heartbeat timeout → promote” mechanism (not full leader election/consensus).
- Client redirection to the newly promoted primary is not implemented yet.

### Server architecture (Phase 1, terminal-based)

Phase 1 focuses on a **single-node server** with clear seams for later replication:

- `ReservationServer` (`server/server.py`):
  - Uses `asyncio.start_server` to accept TCP connections.
  - For each client connection, runs `_handle_client`:
    - Repeatedly reads length-prefixed JSON messages.
    - Dispatches them to `_handle_request`.
    - Writes JSON responses back over the same TCP stream.
- `_handle_request`:
  - Parses `cmd`, `req_id`, `client_id`, `payload`.
  - Implements the core commands:
    - `SEARCH`: read-only; aggregates availability by tier and waiting-list length.
    - `BOOK`:
      - With `tier`: acquires per-tier lock, performs automatic seat allocation or enqueues into waiting list, logs WAL.
      - With `coach`/`seat`: legacy per-seat booking path (retained for compatibility).
    - `CANCEL`: acquires per-tier lock for the ticket’s tier, cancels the ticket, and promotes the next waiting ticket if applicable.
    - `STATUS`: when given `ticket_id`, returns the current state of that ticket; a legacy seat-based mode is also supported.
    - `HOLD` / `RELEASE`: legacy per-seat commands (still supported but not used by the new tier-based terminal client).
    - `HEARTBEAT`: simple liveness probe.
  - Wraps operations in try/except to convert errors into structured error responses.

Although replication is not yet wired in Phase 1, the design leaves clear extension points:

- WAL entries can be streamed to backup nodes.
- Commands like `HEARTBEAT` and `SYNC` can be introduced without changing the framing.

### Client and stress testing (Phase 1, tier-based)

- **Terminal client** (`client/client.py`):
  - Remains terminal-based in Phase 1.
  - Uses the same framing and JSON protocol as the server.
  - Provides a REPL to send:
    - `SEARCH` (by train ID) to view availability and waiting-list length per tier.
    - `BOOK` (by train ID + tier) to request a ticket:
      - Returns a `ticket` object with `status` `confirmed` or `waiting`.
    - `CANCEL` (by `ticket_id`) to cancel a ticket; may trigger promotion from the waiting list.
    - `STATUS` (by `ticket_id`) to inspect the current ticket state.
- **Stress harness** (`client/stress_test.py`):
  - Spawns many asynchronous client workers.
  - Each worker issues random tier-based booking requests, mixing `1AC`, `2AC`, and `3AC`.
  - Occasionally cancels confirmed or waiting tickets to exercise the waiting-list promotion logic.
  - Measures successful confirmed bookings and throughput.

All of these changes are **Phase 1 only** and the system remains **terminal-based**. GUI, additional optimizations, and Phase 2+ features are deliberately not introduced here.


