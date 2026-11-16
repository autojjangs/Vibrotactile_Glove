import asyncio
import csv
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from vibro.engine import Engine
from vibro import protocol as p
from vibro.config import load_route, load_settings, validate_addresses, timeline
from vibro.logging import EventLog
from vibro.transport import SimTransport, BleTransport

ROOT = Path(__file__).resolve().parents[1]
TURN = dict(id="one", direction="left", expected_action="turn", preview_s=1, auto_on_s=4, pass_s=6)
STRAIGHT = dict(TURN, expected_action="straight", cue_s=0)

class MemoryLog:
    def __init__(self):
        self.rows = []
    def write(self, event, **data):
        self.rows.append(dict(event=event, **data))

class RulesTest(unittest.TestCase):
    def setUp(self):
        self.log = MemoryLog()
        self.engine = Engine("baseline", self.log)
        self.seq = 0
    def packet(self, kind, value, side="left", ref=0):
        self.seq += 1
        return p.Packet(kind, side, value, self.seq, self.seq * 10, ref)
    def press(self, side="left", at=2):
        return self.engine.packet(self.packet(p.CONTACT, 1, side), at)
    def release(self, side="left"):
        return self.engine.packet(self.packet(p.CONTACT, 0, side), 2.1)
    def begin(self, condition="baseline", point=TURN):
        self.engine = Engine(condition, self.log)
        self.engine.start()
        self.engine.scheduled("preview", point, 1)
        self.engine.pre_ref = 42
        self.engine.packet(self.packet(p.HAPTIC, p.PREVIEW, ref=42), 1.01)

    def test_preview_requires_first_motor_report(self):
        self.engine.start()
        self.engine.scheduled("preview", TURN, 1)
        self.assertEqual(self.press(), [])
        self.assertEqual(self.log.rows[-1]["reason"], "before_first_vibration")

    def test_preview_start_accepts_input_before_second_pulse(self):
        self.begin()
        effects = self.press(at=1.02)
        self.assertTrue(self.engine.lit)
        self.assertEqual(effects, [("led", "left", True), ("haptic", "left", p.FEEDBACK)])

    def test_stale_preview_cannot_open_window(self):
        self.engine.start()
        self.engine.scheduled("preview", TURN, 1)
        self.engine.pre_ref = 42
        self.engine.packet(self.packet(p.HAPTIC, p.PREVIEW, ref=41), 1.01)
        self.assertFalse(self.engine.allowed)

    def test_hold_is_not_a_second_toggle(self):
        self.begin()
        self.press()
        self.assertEqual(self.press(), [])
        self.assertTrue(self.engine.lit)
        self.assertEqual(self.log.rows[-1]["reason"], "duplicate_state")
        self.release()
        self.press()
        self.assertFalse(self.engine.lit)

    def test_wrong_hand_and_outside_window_are_logged(self):
        self.press()
        self.assertEqual(self.log.rows[-1]["reason"], "trial_not_running")
        self.begin()
        self.press("right")
        self.assertEqual(self.log.rows[-1]["reason"], "wrong_hand")
        self.assertFalse(self.engine.lit)

    def test_pass_automatically_stops_manual_indicator_and_feedback(self):
        self.begin()
        self.press()
        effects = self.engine.scheduled("pass", TURN, 6)
        self.assertEqual(effects, [("led", "left", False), ("haptic", "left", p.STOP)])
        self.assertNotIn("manual_off_missed", [x["event"] for x in self.log.rows])
        self.assertEqual(self.log.rows[-1]["event"], "point_pass")
        self.assertTrue(self.log.rows[-1]["before"]["lit"])
        self.assertIsNone(self.engine.active)
        self.assertFalse(self.engine.lit)
        self.assertFalse(self.engine.allowed)
        self.release()
        self.assertEqual(self.press(at=6.2), [])
        self.assertEqual(self.log.rows[-1]["reason"], "outside_point")

    def test_manual_off_before_pass_remains_available(self):
        self.begin()
        self.press()
        self.release()
        effects = self.press()
        self.assertEqual(effects, [("led", "left", False), ("haptic", "left", p.STOP)])
        self.assertFalse(self.engine.lit)
        self.assertEqual(self.log.rows[-1]["result"], "off")
        self.engine.scheduled("pass", TURN, 6)
        self.assertFalse(self.engine.lit)
        self.assertFalse(self.log.rows[-1]["before"]["lit"])
        self.assertNotIn("manual_off_missed", [x["event"] for x in self.log.rows])

    def test_cancel_is_latched_on_any_trial_type(self):
        self.begin("proposed")
        self.press()
        self.release()
        self.assertEqual(self.press(), [])
        self.assertEqual(self.log.rows[-1]["reason"], "already_cancelled")
        self.assertEqual(self.engine.scheduled("auto", TURN, 4), [])
        self.assertFalse(self.engine.lit)

    def test_straight_ground_truth_does_not_skip_automation(self):
        self.begin("proposed", STRAIGHT)
        effects = self.engine.scheduled("auto", STRAIGHT, 4)
        self.assertTrue(self.engine.lit)
        self.assertIn(("led", "left", True), effects)
        self.press()
        self.assertEqual(self.log.rows[-1]["reason"], "after_automatic_on")
        self.assertTrue(self.engine.lit)

    def test_cancel_reset_and_hold_across_points(self):
        self.begin("proposed")
        self.press()
        self.engine.scheduled("pass", TURN, 6)
        next_point = dict(TURN, id="two", preview_s=7, auto_on_s=9, pass_s=10)
        self.engine.scheduled("preview", next_point, 7)
        self.engine.pre_ref = 43
        self.engine.packet(self.packet(p.HAPTIC, p.PREVIEW, ref=43), 7.01)
        self.press(at=7.1)
        self.assertFalse(self.engine.cancelled)
        self.release()
        self.press(at=7.3)
        self.assertTrue(self.engine.cancelled)

    def test_snapshot_held_contact_is_not_press(self):
        self.begin()
        self.engine.packet(self.packet(p.STATE, 1), 1.1)
        self.press()
        self.assertFalse(self.engine.lit)
        self.release()
        self.press()
        self.assertTrue(self.engine.lit)

    def test_auto_without_warning_ack_aborts(self):
        self.engine = Engine("proposed", self.log)
        self.engine.start()
        self.engine.scheduled("preview", TURN, 1)
        with self.assertRaises(RuntimeError):
            self.engine.scheduled("auto", TURN, 4)

class ConfigurationTest(unittest.TestCase):
    def test_settings_and_macs(self):
        settings = load_settings(ROOT / "config/settings.json")
        self.assertEqual(validate_addresses(settings)[0], "70:AF:09:34:C3:A6")
    def test_draft_route_cannot_accidentally_run(self):
        draft = load_route(ROOT / "routes/demo.json")
        draft["ready"] = False
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "draft_route.json"
            path.write_text(json.dumps(draft), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not finalized"):
                load_route(path)
    def test_demo_events_sorted(self):
        items = timeline(load_route(ROOT / "routes/demo.json"))
        self.assertEqual([x[0] for x in items], sorted(x[0] for x in items))
    def test_late_straight_cue_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "route.json"
            path.write_text(json.dumps(dict(route_id="r", ready=True, duration_s=7,
                                           events=[dict(STRAIGHT, cue_s=2)])))
            with self.assertRaisesRegex(ValueError, "Video cue"):
                load_route(path)
    def test_wire_packet_layout_and_validation(self):
        self.assertEqual(len(p.command(p.PREVIEW, 42, 200, 800, 2)), 12)
        raw = p.EVENT.pack(1, p.CONTACT, 1, 1, 10, 500, 0)
        self.assertEqual(p.decode(raw).side, "right")
        for bad in (b"123", p.EVENT.pack(2, 1, 0, 1, 1, 0, 0),
                    p.EVENT.pack(1, 1, 0, 5, 1, 0, 0)):
            with self.assertRaises(ValueError):
                p.decode(bad)
    def test_csv_flush_and_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            log = EventLog(root, "P01", "01", "baseline", "r", echo=False)
            log.origin = time.monotonic()
            log.write("input", side="left", result="ignored", reason="wrong_hand",
                      before={"lit": False}, device_ms=42, sequence=2)
            path = log.directory / "events.csv"
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["participant"], "P01")
            self.assertEqual(rows[0]["reason"], "wrong_hand")
            self.assertEqual(rows[0]["device_ms"], "42")
            log.close()

class TransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = load_settings(ROOT / "config/settings.json")
        self.settings["haptics"]["preview"] = dict(on_ms=5, off_ms=5, count=2)
        self.queue = asyncio.Queue()
        self.log = MemoryLog()

    async def test_feedback_replaces_pending_preview_completion(self):
        transport = SimTransport(self.settings, self.queue, self.log)
        await transport.connect()
        await transport.send("left", p.PREVIEW)
        feedback_ref = await transport.send("left", p.FEEDBACK)
        await asyncio.sleep(.04)
        packets = []
        while not self.queue.empty():
            packets.append(self.queue.get_nowait()[1])
        modes = [(x.value, x.reference) for x in packets if x.kind == p.HAPTIC]
        self.assertIn((p.FEEDBACK, feedback_ref), modes)
        self.assertNotIn(p.STOP, [m[0] for m in modes])
        await transport.close()

    async def test_simulated_hold_emits_only_press_and_release(self):
        transport = SimTransport(self.settings, self.queue, self.log)
        transport.inject("left", True)
        transport.inject("left", True)
        transport.inject("left", False)
        self.assertEqual(self.queue.qsize(), 2)
        await transport.close()

    async def test_sequence_gap_and_wrong_hand_fault(self):
        transport = BleTransport(self.settings, self.queue, self.log)
        transport.receive("left", p.EVENT.pack(1, p.CONTACT, 0, 1, 1, 10, 0))
        transport.receive("left", p.EVENT.pack(1, p.CONTACT, 0, 0, 3, 20, 0))
        transport.receive("right", p.EVENT.pack(1, p.STATE, 0, 0, 4, 30, 0))
        items = []
        while not self.queue.empty():
            items.append(self.queue.get_nowait())
        self.assertEqual(sum(x[0] == "fault" for x in items), 2)

    async def test_gatt_write_alone_is_not_application_ack(self):
        transport = BleTransport(self.settings, self.queue, self.log)
        transport.settings["ble"]["command_timeout_s"] = .02
        transport.clients["left"] = type("Client", (), {"write_gatt_char": AsyncMock()})()
        with self.assertRaises(TimeoutError):
            await transport.send("left", p.PREVIEW)
        self.assertEqual(transport.waiters, {})

    async def test_matching_application_ack_completes_command(self):
        transport = BleTransport(self.settings, self.queue, self.log)
        async def write(_uuid, data, response):
            _, op, ref, *_ = p.COMMAND.unpack(data)
            transport.receive("left", p.EVENT.pack(1, p.ACK, 0, op, 1, 100, ref))
        transport.clients["left"] = type("Client", (), {})()
        transport.clients["left"].write_gatt_char = write
        self.assertEqual(await transport.send("left", p.PREVIEW), 1)

if __name__ == "__main__":
    unittest.main()
