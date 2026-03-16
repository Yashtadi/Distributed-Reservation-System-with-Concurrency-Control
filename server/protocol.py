import asyncio
import json
from typing import Any, Dict


class ProtocolError(Exception):
    pass


async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    data = await reader.readexactly(n)
    return data


async def read_message(reader: asyncio.StreamReader) -> Dict[str, Any]:
    """
    Read a single length-prefixed JSON message from the stream.
    4-byte big-endian length header followed by JSON payload.
    """
    header = await read_exactly(reader, 4)
    length = int.from_bytes(header, byteorder="big")
    if length <= 0 or length > 10_000_000:
        raise ProtocolError(f"Invalid frame length: {length}")
    payload = await read_exactly(reader, length)
    try:
        msg = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise ProtocolError(f"Invalid JSON payload: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("Message must be a JSON object")
    return msg


async def write_message(writer: asyncio.StreamWriter, message: Dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = len(payload).to_bytes(4, byteorder="big")
    writer.write(header + payload)
    await writer.drain()
