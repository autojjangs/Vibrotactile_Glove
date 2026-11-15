import asyncio
import time
from . import protocol as p
from .config import validate_addresses

class BleTransport:
    def __init__(self, settings, queue, log):
        self.settings, self.queue, self.log = settings, queue, log
        self.clients, self.last_sequence, self.waiters = {}, {}, {}
        self.locks = {side: asyncio.Lock() for side in p.SIDES}
        self.next_ref = 0
        self.closing = False
        self.heartbeat_task = None

    def fault(self, reason):
        if not self.closing:
            self.queue.put_nowait(("fault", reason, time.monotonic()))

    def receive(self, side, data):
        at = time.monotonic()
        try:
            packet = p.decode(data)
            if packet.side != side:
                raise ValueError(f"Wrong hand firmware: expected {side}, received {packet.side}")
            last = self.last_sequence.get(side)
            self.last_sequence[side] = packet.sequence
            self.log.write("ble_rx", received=at, side=side, device_ms=packet.device_ms,
                           sequence=packet.sequence, reference=packet.reference,
                           details={"kind": packet.kind, "value": packet.value, "hex": bytes(data).hex()})
            if last is not None and packet.sequence != ((last + 1) & 0xffffffff):
                self.fault(f"{side}: notification sequence gap {last} -> {packet.sequence}")
            self.queue.put_nowait(("packet", packet, at))
            if packet.kind == p.ACK:
                future = self.waiters.get((side, packet.reference))
                if future is not None and not future.done():
                    future.set_result(packet.value)
            elif packet.kind == p.ERROR:
                self.fault(f"{side}: firmware error {packet.value}")
        except Exception as error:
            self.log.write("ble_invalid_packet", received=at, side=side, reason=str(error),
                           details=bytes(data).hex())
            self.fault(f"{side}: {error}")

    async def connect(self):
        from bleak import BleakClient, BleakScanner
        addresses = validate_addresses(self.settings)
        self.log.write("ble_scan_start")
        found = await BleakScanner.discover(timeout=6)
        by_address = {device.address.lower(): device for device in found}
        for side, address in zip(p.SIDES, addresses):
            device = by_address.get(address.lower())
            if device is None:
                raise RuntimeError(f"{side} glove {address} not found; check power and firmware")
            def disconnected(_client, hand=side):
                self.log.write("ble_disconnected", side=hand)
                self.fault(f"{hand}: disconnected")
            client = BleakClient(device, disconnected_callback=disconnected)
            self.clients[side] = client
            await client.connect()
            await client.start_notify(p.EVENT_UUID, lambda _, data, hand=side: self.receive(hand, data))
            self.log.write("ble_connected", side=side, details={"address": address})
            await self.send(side, p.STOP)
            await self.send(side, p.SNAPSHOT)
        self.heartbeat_task = asyncio.create_task(self.heartbeat())

    async def send(self, side, op):
        async with self.locks[side]:
            self.next_ref = self.next_ref % 0xffffffff + 1
            ref = self.next_ref
            on = off = count = 0
            if op in (p.PREVIEW, p.FEEDBACK):
                pattern = self.settings["haptics"]["preview" if op == p.PREVIEW else "feedback"]
                on, off = pattern["on_ms"], pattern["off_ms"]
                count = pattern["count"] if op == p.PREVIEW else 0
            data = p.command(op, ref, on, off, count)
            future = asyncio.get_running_loop().create_future()
            self.waiters[(side, ref)] = future
            self.log.write("ble_command", side=side, reference=ref,
                           details={"opcode": op, "on_ms": on, "off_ms": off, "count": count})
            try:
                async with asyncio.timeout(self.settings["ble"]["command_timeout_s"]):
                    await self.clients[side].write_gatt_char(p.COMMAND_UUID, data, response=True)
                    ack_op = await future
                    if ack_op != op:
                        raise RuntimeError(f"{side}: mismatched command acknowledgement")
            finally:
                self.waiters.pop((side, ref), None)
            return ref

    async def heartbeat(self):
        try:
            while True:
                await asyncio.sleep(self.settings["ble"]["heartbeat_s"])
                for side in p.SIDES:
                    await self.send(side, p.PING)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fault(f"heartbeat: {error}")

    async def close(self):
        self.closing = True
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
        for side, client in self.clients.items():
            if client.is_connected:
                try:
                    await self.send(side, p.STOP)
                except Exception as error:
                    self.log.write("shutdown_command_failed", side=side, reason=str(error))
                try:
                    await client.disconnect()
                except Exception as error:
                    self.log.write("disconnect_failed", side=side, reason=str(error))

class SimTransport:
    def __init__(self, settings, queue, log):
        self.settings, self.queue, self.log = settings, queue, log
        self.next_ref = 0
        self.sequence = dict.fromkeys(p.SIDES, 0)
        self.contact = dict.fromkeys(p.SIDES, False)
        self.tasks = {}
        self.started = time.monotonic()

    def emit(self, side, kind, value, ref=0):
        self.sequence[side] += 1
        packet = p.Packet(kind, side, value, self.sequence[side],
                          int((time.monotonic() - self.started) * 1000), ref)
        at = time.monotonic()
        self.log.write("sim_rx", received=at, side=side, device_ms=packet.device_ms,
                       sequence=packet.sequence, reference=ref, details={"kind": kind, "value": value})
        self.queue.put_nowait(("packet", packet, at))

    async def connect(self):
        for side in p.SIDES:
            self.log.write("sim_connected", side=side)
            self.emit(side, p.STATE, 0)

    async def send(self, side, op):
        self.next_ref += 1
        ref = self.next_ref
        self.log.write("sim_command", side=side, reference=ref, details={"opcode": op})
        if op == p.SNAPSHOT:
            self.emit(side, p.STATE, int(self.contact[side]), ref)
        elif op in (p.STOP, p.PREVIEW, p.FEEDBACK):
            task = self.tasks.pop(side, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.emit(side, p.HAPTIC, op, ref)
            if op == p.PREVIEW:
                pattern = self.settings["haptics"]["preview"]
                seconds = (pattern["on_ms"] * pattern["count"] + pattern["off_ms"] * (pattern["count"] - 1)) / 1000
                async def finish():
                    await asyncio.sleep(seconds)
                    self.emit(side, p.HAPTIC, p.STOP, ref)
                self.tasks[side] = asyncio.create_task(finish())
        self.emit(side, p.ACK, op, ref)
        return ref

    def inject(self, side, pressed):
        if self.contact[side] != pressed:
            self.contact[side] = pressed
            self.emit(side, p.CONTACT, int(pressed))
        else:
            self.log.write("sim_hold", side=side, reason="not_a_new_contact")

    async def close(self):
        for side in p.SIDES:
            await self.send(side, p.STOP)
