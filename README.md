# Distributed Reservation System with Concurrency Control

A Phase 1 prototype of a distributed train reservation system with tier-based booking, waiting-list promotion, simple write-ahead logging, and a replication skeleton.

## Overview

This repository implements a reservation system that supports:
- Booking seats by **train and tier** (`1AC`, `2AC`, `3AC`)
- Automatic allocation of available seats
- FIFO waiting lists for fully booked tiers
- Cancellation with automatic promotion from the waiting list
- A simple file-backed **WAL** (`data/wal.log`) for durability and recovery
- A **primary/backup replication skeleton** using a separate replication port
- A terminal-based client and a Streamlit web UI

## Key Components

- `server/`
  - `server.py`: Async TCP server and request dispatcher
  - `db.py`: In-memory train/coach/seat model, ticket lifecycle, waiting lists, WAL replay
  - `lock_manager.py`: Fine-grained per-seat and per-tier lock management
  - `protocol.py`: Length-prefixed JSON framing and request/response conventions
  - `replication.py`: Primary and backup replication primitives
- `client/`
  - `client.py`: Async terminal client supporting `SEARCH`, `BOOK`, `CANCEL`, `STATUS`
  - `stress_harness.py`: High-load reservation stress testing harness
- `streamlit_app.py`: Streamlit-based user interface for interactive bookings
- `docs/`
  - `DESIGN.md`: Design rationale, data model, concurrency, durability, and replication notes
  - `PROTOCOL.md`: Custom client-server protocol specification

## Features

### Tier-based booking
- Clients request a ticket for a specific train and tier.
- If a seat is available, the server confirms the booking.
- If no seat is available, the request is placed on a tier-specific waiting list.
- Waiting list promotions happen automatically when a confirmed ticket is cancelled.

### Concurrency control
- Per-tier locks serialize tier booking and cancellation logic.
- Per-seat locks support legacy hold/book/release operations.
- The lock manager prevents double-booking and promotes consistent seat state.

### Durability
- A simple WAL persists state-changing operations as newline-delimited JSON.
- On startup, the server replays the WAL and restores the in-memory database.
- If the WAL is empty, a demo configuration with trains `T1` and `T2` is initialized.

### Replication support
- Primary server broadcasts WAL entries and heartbeats over a replication socket.
- Backup server connects to the primary, applies WAL entries, and can promote itself if heartbeats stop.
- This is a Phase 1 skeleton, not a full consensus implementation.

## Getting Started

### Setup

```bash
pip install -r requirements.txt
```

### Start the server

```bash
python -m server.server --host 127.0.0.1 --port 9000
```

Optional primary/backup flags:

```bash
python -m server.server --role primary --host 127.0.0.1 --port 9000 --repl-port 9001
python -m server.server --role backup --host 127.0.0.1 --port 9000 --primary-repl-host 127.0.0.1 --primary-repl-port 9001
```

### Run the terminal client

```bash
python -m client.client --host 127.0.0.1 --port 9000 --client-id c1
```

### Run the Streamlit UI

```bash
streamlit run streamlit_app.py
```

## Supported Operations

- `SEARCH`: Query tier availability and waiting-list length for a train.
- `BOOK`: Reserve a ticket by tier, with automatic allocation or waiting-list entry.
- `CANCEL`: Cancel a ticket and promote the next waiting ticket if possible.
- `STATUS`: Inspect the current state of a ticket.
- `HEARTBEAT`: Health check for server liveness.

## Protocol

The client-server protocol uses TCP with length-prefixed JSON messages. See `docs/PROTOCOL.md` for full request/response structure and command details.

## Testing

Run the test suite with:

```bash
pytest
```

## Notes

- The repository is implemented in Python and uses `asyncio` for server and client networking.
- The Streamlit UI is a convenience layer on top of the same protocol used by the terminal client.
- The design intentionally separates tier-based booking from legacy per-seat booking for future extension.

## Repository Structure

- `server/`: core server and persistence logic
- `client/`: client interface and stress harness
- `data/`: WAL and runtime data storage
- `docs/`: design and protocol documentation
- `streamlit_app.py`: browser-based UI

## Further Development

Potential future improvements include:
- full leader election and client redirection in replication
- persistent storage beyond WAL replay
- richer train and coach configuration
- reservation timeouts and hold expiries exposed via the API
