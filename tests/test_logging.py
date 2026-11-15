import csv
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from vibro import protocol as p
from vibro.engine import Engine
from vibro.logging import EventLog


class ReadableLogTest(unittest.TestCase):
    def test_filter_preserves_all_csv_data_and_visible_inputs(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as output:
            log = EventLog(root, "P01", "01", "baseline", "demo")
            log.origin = time.monotonic()
            try:
                log.write("ble_command", side="left", reference=9, details={"opcode": p.PING})
                log.write("ble_rx", side="left", sequence=20, details={"kind": p.ACK})
                log.write("input", side="left", input="contact", result="on",
                          before={"lit": False}, after={"lit": True}, device_ms=1200)
                log.write("input", side="left", input="release", result="recorded", reason="release_no_action")
                log.write("input", side="left", input="contact", result="off")
                with (log.directory / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                readable = (log.directory / "experiment.log").read_text(encoding="utf-8")
                self.assertEqual(len(rows), 5)
                self.assertEqual(rows[0]["reference"], "9")
                self.assertEqual(json.loads(rows[2]["after"]), {"lit": True})
                self.assertEqual(rows[2]["device_ms"], "1200")
                self.assertEqual(readable, output.getvalue())
                self.assertEqual(len(readable.splitlines()), 3)
                self.assertIn("전극 접촉 → 수동 점등", readable)
                self.assertIn("전극 떼기", readable)
                self.assertIn("전극 접촉 → 수동 소등", readable)
                self.assertNotIn("ble_command", readable)
                self.assertNotIn("ble_rx", readable)
            finally:
                log.close()

    def test_proposed_cancellation_stays_clear_through_point_pass(self):
        with tempfile.TemporaryDirectory() as root:
            log = EventLog(root, "P01", "01", "proposed", "demo", echo=False)
            log.origin = 0
            try:
                engine = Engine("proposed", log)
                point = dict(id="right_test", direction="right", expected_action="straight")
                engine.start()
                engine.scheduled("preview", point, 5)
                engine.pre_ref = 42
                engine.packet(p.Packet(p.HAPTIC, "right", p.PREVIEW, 1, 5000, 42), 5.01)
                engine.packet(p.Packet(p.CONTACT, "right", 1, 2, 5400, 0), 5.4)
                engine.packet(p.Packet(p.CONTACT, "right", 0, 3, 5600, 0), 5.6)
                engine.packet(p.Packet(p.CONTACT, "right", 1, 4, 6000, 0), 6)
                engine.scheduled("auto", point, 7)
                engine.scheduled("pass", point, 8)
                readable = (log.directory / "experiment.log").read_text(encoding="utf-8")
                self.assertIn("오른쪽 사전 안내 시작", readable)
                self.assertIn("오른쪽 전극 접촉 → 자동 점등 취소", readable)
                self.assertIn("이미 취소됨", readable)
                self.assertIn("자동 점등하지 않음", readable)
                self.assertNotIn("방향지시등 자동 점등", readable)
                self.assertIn("입력 구간 종료", readable)
            finally:
                log.close()

    def test_ignored_inputs_and_errors_are_never_suppressed(self):
        with tempfile.TemporaryDirectory() as root:
            log = EventLog(root, "P01", "01", "baseline", "demo", echo=False)
            try:
                for reason in ("trial_not_running", "outside_point", "before_first_vibration",
                               "wrong_hand", "duplicate_state", "already_cancelled", "after_automatic_on"):
                    log.write("input", side="right", input="contact", result="ignored", reason=reason)
                log.write("ble_invalid_packet", side="right", reason="bad packet")
                log.write("trial_aborted", reason="left: disconnected", before={"running": True})
                log.write("shutdown_command_failed", side="left", reason="timeout")
                log.write("future_hardware_error", reason="new diagnostic")
                readable = (log.directory / "experiment.log").read_text(encoding="utf-8")
                self.assertEqual(readable.count("전극 접촉 → 무시:"), 7)
                self.assertIn("안내 방향과 다른 손", readable)
                self.assertIn("bad packet", readable)
                self.assertIn("left: disconnected", readable)
                self.assertIn("timeout", readable)
                self.assertIn("new diagnostic", readable)
            finally:
                log.close()

    def test_verbose_adds_diagnostics_only_to_console(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as output:
            log = EventLog(root, "P01", "01", "baseline", "demo", verbose=True)
            try:
                log.write("ble_command", side="left", details={"opcode": p.PING})
                log.write("operator_status", after={"running": True, "point": "one",
                                                   "allowed": True, "lit": True, "cancelled": False})
                readable = (log.directory / "experiment.log").read_text(encoding="utf-8")
                self.assertIn("ble_command", output.getvalue())
                self.assertNotIn("ble_command", readable)
                self.assertIn("방향지시등: 켜짐", readable)
                self.assertIn("수동 점등/소등 가능", readable)
            finally:
                log.close()
