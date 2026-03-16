## Protocol Specification

This document specifies the custom request–response protocol used between clients and the reservation server in Phase 1.

### Transport and framing

- **Transport**: TCP.
- **Framing**: length-prefixed JSON:
  - 4-byte big-endian unsigned integer: length of the JSON payload in bytes.
  - Followed immediately by the UTF-8 encoded JSON payload.
- Multiple messages can be sent on the same connection; the server processes them sequentially.

Example (conceptual):

- Client sends:
  - `[00 00 00 2a]` (length = 42 bytes)
  - `{"cmd":"HEARTBEAT","req_id":"r1","client_id":"C1","payload":{}}`

Framing helpers are implemented in `server/protocol.py`:

- `read_message(reader) -> dict`
- `write_message(writer, message: dict) -> None`

### Common message structure

#### Request

All requests are JSON objects with the following fields:

- `cmd` (string): command name, e.g. `"SEARCH"`, `"HOLD"`.
- `req_id` (string): client-generated unique identifier for the request.
- `client_id` (string): logical client identifier, used for hold ownership.
- `payload` (object): command-specific arguments.
- `ts` (number): client timestamp (seconds since epoch).

Example:

```json
{
  "cmd": "SEARCH",
  "req_id": "r1",
  "client_id": "C1",
  "payload": {
    "train": "T1"
  },
  "ts": 1710000000.123
}
```

#### Response

All responses are JSON objects with the following fields:

- `req_id` (string): echoes the request ID.
- `status` (string): `"OK"` or `"ERROR"`.
- `code` (string): machine-readable status or error code, e.g. `"OK"`, `"LOCK_TIMEOUT"`, `"HOLD_FAILED"`.
- `data` (object, optional): response-specific payload when `status == "OK"`.
- `message` (string, optional): human-readable error message when `status == "ERROR"`.
- `ts` (number): server timestamp (seconds since epoch).

Example success:

```json
{
  "req_id": "r1",
  "status": "OK",
  "code": "OK",
  "data": {
    "train": "T1",
    "coaches": [
      { "coach": "A", "available_seats": 20 },
      { "coach": "B", "available_seats": 20 }
    ]
  },
  "ts": 1710000000.456
}
```

Example error:

```json
{
  "req_id": "r2",
  "status": "ERROR",
  "code": "HOLD_FAILED",
  "message": "seat not available",
  "ts": 1710000000.789
}
```

### Commands (Phase 1 with tiers and waiting list)

#### `SEARCH`

- **Purpose**: Query seat availability for a train by tier.
- **Request payload**:
  - `train` (string): train ID, e.g. `"T1"`.
- **Response `data`**:
  - `train` (string)
  - `tiers` (array):
    - Each element:
      - `tier` (string): `"1AC"`, `"2AC"`, or `"3AC"`.
      - `available_seats` (integer): number of currently `available` seats in that tier.
      - `waiting_list_length` (integer): number of tickets currently in the waiting list for that `(train, tier)`.

Notes:

- `available_seats` counts seats currently in `available` state; `held` and `booked` are excluded.
- Waiting list length reflects tickets with `status="waiting"` for that train and tier.

#### `BOOK` (tier-based automatic seat allocation)

- **Purpose**: Request a ticket in a given tier; the server automatically assigns a seat if possible or enqueues the request into a waiting list.
- **Request payload**:
  - `train` (string)
  - `tier` (string): `"1AC"`, `"2AC"`, or `"3AC"`.
- **Response `data`**:
  - `ticket` (object):
    - `ticket_id` (string)
    - `client_id` (string)
    - `train_id` (string)
    - `tier` (string)
    - `seat_id` (string or `null`)
    - `status` (string): `"confirmed"` or `"waiting"` at creation time.
    - `created_at` (number)
  - `ticket_status` (string): `"confirmed"` or `"waiting"`.
  - `waiting_position` (integer, optional): present only when the ticket is placed on the waiting list, representing its 1-based position in the queue.

Errors:

- `LOCK_TIMEOUT`: tier-level lock could not be acquired within the timeout.
- `BOOK_FAILED`: booking validation failed (e.g., invalid tier/train).

Semantics:

- If a seat is available in the requested tier:
  - The server allocates it to the client, creates a ticket with `status="confirmed"`, and marks the seat `booked`.
- If no seat is available:
  - The server creates a ticket with `status="waiting"` and appends it to the FIFO waiting list for `(train, tier)`.

> The legacy per-seat `BOOK` mode (with `train`/`coach`/`seat` payload) is still supported internally but no longer used by the Phase 1 terminal client.

#### `CANCEL`

- **Purpose**: Cancel an existing ticket, potentially promoting a waiting-list ticket.
- **Request payload**:
  - `ticket_id` (string)
- **Response `data`**:
  - `cancelled_ticket` (object): the final state of the cancelled ticket.
  - `promoted_ticket` (object, optional): present if a waiting ticket was promoted to confirmed status due to the cancellation.

Errors:

- `NOT_FOUND`: ticket does not exist.
- `CANCEL_FAILED`: validation failure (e.g., ticket does not belong to this client).
- `LOCK_TIMEOUT`: tier-level lock could not be acquired.

Semantics:

- If the ticket is `confirmed`:
  - Its seat is freed (seat becomes `available`).
  - The ticket’s status becomes `cancelled`.
  - If the waiting list for that `(train, tier)` is non-empty:
    - The first ticket in the queue is promoted:
      - It is assigned the freed seat.
      - Its status becomes `confirmed`.
- If the ticket is `waiting`:
  - It is removed from the waiting list and marked `cancelled`.

#### `STATUS` (ticket-oriented)

- **Purpose**: Inspect the status of a ticket.
- **Request payload**:
  - `ticket_id` (string)
- **Response `data`**:
  - The full serialized ticket object, for example:

```json
{
  "ticket_id": "TKT-1710000000123-1",
  "client_id": "C1",
  "train_id": "T1",
  "tier": "2AC",
  "seat_id": "T1-2A-5",
  "status": "confirmed",
  "created_at": 1710000000.123
}
```

> For backward compatibility, a legacy seat-based `STATUS` mode (with `train`/`coach`/`seat`) remains available but is not used by the new Phase 1 tier-based client.

#### `HEARTBEAT`

- **Purpose**: Health check to verify the server is alive and responsive.
- **Request payload**:
  - Empty object `{}`.
- **Response `data`**:
  - `{ "status": "alive" }`

### Error handling and invalid messages

- If the server encounters malformed JSON or invalid framing, it treats this as a protocol error and closes the connection.
- If `cmd` is unknown, the server returns:
  - `status: "ERROR"`, `code: "UNKNOWN_CMD"`.
- Unexpected internal errors are surfaced as:
  - `status: "ERROR"`, `code: "SERVER_ERROR"`, with a descriptive `message`.

### Extensibility

The protocol is designed to be forward-compatible:

- New commands (e.g. `SYNC`, `CANCEL`) can be added with new `cmd` values and payload shapes.
- Existing clients ignore any unknown fields in responses.
- The length-prefixed JSON framing remains unchanged across phases.

