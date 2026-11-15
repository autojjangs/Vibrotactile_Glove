"""Versioned BLE packets; all packets fit the default 20-byte ATT payload."""
import struct
from dataclasses import dataclass
VERSION = 1
SERVICE_UUID = "e5f90001-730d-4d25-9c25-51ee70b7d820"
COMMAND_UUID = "e5f90002-730d-4d25-9c25-51ee70b7d820"
EVENT_UUID = "e5f90003-730d-4d25-9c25-51ee70b7d820"
STOP, PREVIEW, FEEDBACK, SNAPSHOT, PING = range(5)
CONTACT, ACK, STATE, HAPTIC, ERROR = range(1, 6)
COMMAND = struct.Struct("<BBIHHH")
EVENT = struct.Struct("<BBBBIII")
SIDES = ("left", "right")

def command(op, ref, on_ms=0, off_ms=0, count=0):
    return COMMAND.pack(VERSION, op, ref, on_ms, off_ms, count)

@dataclass(frozen=True)
class Packet:
    kind: int
    side: str
    value: int
    sequence: int
    device_ms: int
    reference: int

def decode(data):
    if len(data) != EVENT.size:
        raise ValueError(f"event length {len(data)} != {EVENT.size}")
    version, kind, side, value, seq, ms, ref = EVENT.unpack(data)
    if version != VERSION or side > 1 or kind not in (CONTACT, ACK, STATE, HAPTIC, ERROR):
        raise ValueError("unsupported version, side or event kind")
    if kind in (CONTACT, STATE) and value not in (0, 1):
        raise ValueError("invalid contact state")
    if kind == HAPTIC and value not in (STOP, PREVIEW, FEEDBACK):
        raise ValueError("invalid haptic mode")
    return Packet(kind, SIDES[side], value, seq, ms, ref)
