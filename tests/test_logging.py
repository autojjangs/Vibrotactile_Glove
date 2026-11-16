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



class LogOwnershipTest(unittest.TestCase):
    def test_owner_is_only_selected_for_a_valid_sudo_user(self):
        from unittest.mock import patch
        from vibro import logging as module
        with patch.object(module.sys, "platform", "linux"), \
             patch.object(module.os, "geteuid", return_value=0, create=True), \
             patch.dict(module.os.environ, {"SUDO_UID": "1000", "SUDO_GID": "1001"}, clear=True):
            self.assertEqual(module.sudo_log_owner(), (1000, 1001))
            with patch.object(module.os, "geteuid", return_value=1000):
                self.assertIsNone(module.sudo_log_owner())
            for env in ({}, {"SUDO_UID": "0", "SUDO_GID": "0"},
                        {"SUDO_UID": "bad", "SUDO_GID": "1001"}):
                with patch.dict(module.os.environ, env, clear=True):
                    self.assertIsNone(module.sudo_log_owner())

    def test_new_log_directory_and_session_files_belong_to_invoking_user(self):
        from pathlib import Path
        from unittest.mock import patch
        from vibro import logging as module
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(module, "sudo_log_owner", return_value=(1000, 1001)), \
             patch.object(module.os, "chown", create=True) as chown, \
             patch.object(module.os, "fchown", create=True) as fchown:
            root = Path(folder) / "logs"
            log = EventLog(root, "TEST", "01", "baseline", "demo", echo=False)
            try:
                log.snapshot({}, {})
                self.assertEqual([call.args[0] for call in chown.call_args_list],
                                 [root, log.directory, log.directory / "session.json"])
                self.assertTrue(all(call.args[1:] == (1000, 1001) for call in chown.call_args_list))
                self.assertTrue(all(call.kwargs == {"follow_symlinks": False} for call in chown.call_args_list))
                self.assertEqual(fchown.call_count, 2)
                self.assertTrue(all(call.args[1:] == (1000, 1001) for call in fchown.call_args_list))
            finally:
                log.close()

    def test_existing_log_root_and_previous_data_are_not_changed(self):
        from pathlib import Path
        from unittest.mock import patch
        from vibro import logging as module
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(module, "sudo_log_owner", return_value=(1000, 1001)), \
             patch.object(module.os, "chown", create=True) as chown, \
             patch.object(module.os, "fchown", create=True):
            root = Path(folder)
            old = root / "previous.csv"
            old.write_text("original experiment data", encoding="utf-8")
            log = EventLog(root, "TEST", "02", "proposed", "demo", echo=False)
            try:
                self.assertEqual([call.args[0] for call in chown.call_args_list], [log.directory])
                self.assertEqual(old.read_text(encoding="utf-8"), "original experiment data")
            finally:
                log.close()


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

    def test_baseline_pass_is_normal_automatic_off_without_missed_action(self):
        with tempfile.TemporaryDirectory() as root:
            log = EventLog(root, "TEST", "01", "baseline", "demo", echo=False)
            log.origin = 0
            try:
                engine = Engine("baseline", log)
                point = dict(id="left_test", direction="left", expected_action="turn")
                engine.start()
                engine.scheduled("preview", point, 1)
                engine.pre_ref = 42
                engine.packet(p.Packet(p.HAPTIC, "left", p.PREVIEW, 1, 1000, 42), 1.01)
                engine.packet(p.Packet(p.CONTACT, "left", 1, 2, 2000, 0), 2)
                engine.scheduled("pass", point, 6)
                readable = (log.directory / "experiment.log").read_text(encoding="utf-8")
                self.assertIn("수동 점등", readable)
                self.assertIn("안내 지점 통과 → 자동 소등 · 진동 정지", readable)
                self.assertNotIn("누락", readable)
                with (log.directory / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[-1]["event"], "point_pass")
                self.assertNotIn("manual_off_missed", [row["event"] for row in rows])
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

    def test_video_ready_names_the_actual_requested_playback_mode(self):
        from vibro.logging import format_experiment_event
        for mode, description in (("no", "소프트웨어 재생 (가속 끔)"),
                                  ("auto", "하드웨어 가속 자동 선택")):
            line = format_experiment_event({"event": "video_ready", "details": {"hwdec": mode, "gpu_api": "opengl"}})
            self.assertIn(description, line)
            self.assertNotIn("화면 갱신 유지", line)
            self.assertIn("OpenGL 화면 출력", line)
            self.assertIn("start", line)

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
