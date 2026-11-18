import asyncio
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from vibro import contact_practice as practice, launcher, protocol as p
from vibro.__main__ import parser, run
from vibro.config import load_settings
from vibro.led import LedOutput
from vibro.transport import SimTransport
from tests.test_experiment import MemoryLog

ROOT = Path(__file__).resolve().parents[1]


class ContactControlsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = load_settings(ROOT / "config/settings.json")
        self.queue, self.log = asyncio.Queue(), MemoryLog()
        self.transport = SimTransport(self.settings, self.queue, self.log)
        self.led = LedOutput(self.settings, True, self.log, self.fail)
        self.controls = practice.PracticeControls(self.transport, self.led, self.log, "baseline",
                                                   practice.configuration(self.settings))

    async def asyncTearDown(self):
        await self.led.stop()
        await self.transport.close()

    async def drain(self):
        while not self.queue.empty():
            kind, packet, received = self.queue.get_nowait()
            if kind == "packet":
                await self.controls.packet(packet, received)

    async def touch(self, side, pressed):
        self.transport.inject(side, pressed)
        await self.drain()

    def inputs(self):
        return [row for row in self.log.rows if row["event"] == "input"]

    async def test_manual_both_sides_toggle_preempts_preview_and_pass_turns_off(self):
        for side, key in (("left", "1"), ("right", "2")):
            await self.controls.command(key)
            await self.drain()
            point = self.controls.engine.active
            await self.touch(side, True)
            self.assertEqual(self.led.direction, side)
            commands = [r for r in self.log.rows if r["event"] == "sim_command"]
            self.assertEqual(commands[-1]["details"]["opcode"], p.FEEDBACK)
            self.assertNotIn(side, self.transport.tasks)  # remaining preview stopped
            await self.touch(side, False)
            self.assertEqual(self.led.direction, side)
            await self.touch(side, True)
            self.assertIsNone(self.led.direction)
            await self.touch(side, False)
            await self.touch(side, True)
            await self.controls.advance(point["pass_s"])
            self.assertIsNone(self.led.direction)
            self.assertIsNone(self.controls.engine.active)
            self.assertFalse(self.controls.pending)
            self.assertFalse(self.transport.tasks)
            await self.touch(side, False)
        self.assertEqual([r["result"] for r in self.inputs() if r["input"] == "contact"],
                         ["on", "off", "on"] * 2)

    async def test_early_held_and_wrong_hand_contacts_do_not_activate(self):
        await self.touch("left", True)
        self.assertEqual(self.inputs()[-1]["reason"], "outside_point")
        await self.controls.command("1")
        await self.drain()
        await self.touch("left", True)
        self.assertIsNone(self.led.direction)
        await self.touch("right", True)
        self.assertEqual(self.inputs()[-1]["reason"], "wrong_hand")
        await self.touch("left", False)
        await self.touch("left", True)
        self.assertEqual(self.led.direction, "left")
        await self.controls.command("r")
        await self.drain()
        await self.touch("left", True)
        self.assertIsNone(self.led.direction)
        await self.touch("left", False)
        await self.touch("left", True)
        self.assertEqual(self.led.direction, "left")

    async def test_input_waits_for_matching_first_vibration_report(self):
        await self.controls.command("1")
        packet = p.Packet(p.CONTACT, "left", 1, 1, 0, 0)
        await self.controls.packet(packet, time.monotonic())
        self.assertEqual(self.inputs()[-1]["reason"], "before_first_vibration")
        wrong_ack = p.Packet(p.HAPTIC, "left", p.PREVIEW, 2, 1, self.controls.engine.pre_ref + 100)
        await self.controls.packet(wrong_ack, time.monotonic())
        self.assertFalse(self.controls.engine.allowed)
        await self.drain()
        self.assertTrue(self.controls.engine.allowed)
        await self.controls.packet(packet, time.monotonic())
        self.assertEqual(self.inputs()[-1]["reason"], "duplicate_state")
        self.assertIsNone(self.led.direction)

    async def test_proposed_cancel_is_latched_and_next_round_can_auto_light(self):
        await self.controls.command("p")
        await self.controls.command("2")
        await self.drain()
        point = self.controls.engine.active
        await self.touch("right", True)
        self.assertEqual(self.inputs()[-1]["result"], "cancel")
        self.assertIn("right", self.transport.tasks)  # cancellation keeps preview
        await self.touch("right", False)
        await self.touch("right", True)
        self.assertEqual(self.inputs()[-1]["reason"], "already_cancelled")
        await self.controls.advance(point["auto_on_s"])
        self.assertIsNone(self.led.direction)
        self.assertTrue(any(r["event"] == "auto_suppressed" for r in self.log.rows))
        await self.controls.advance(point["pass_s"])
        await self.controls.command("r")
        await self.drain()
        self.assertFalse(self.controls.engine.cancelled)
        point = self.controls.engine.active
        await self.controls.advance(point["auto_on_s"])
        self.assertEqual(self.led.direction, "right")
        await self.touch("right", False)
        await self.touch("right", True)
        self.assertEqual(self.inputs()[-1]["reason"], "after_automatic_on")
        await self.controls.advance(point["pass_s"])
        self.assertIsNone(self.led.direction)

    async def test_switch_and_stop_clear_deadlines_and_outputs_preserve_held_state(self):
        await self.controls.command("1")
        await self.drain()
        await self.touch("left", True)
        old_end = self.controls.engine.active["pass_s"]
        await self.controls.command("p")
        self.assertIsNone(self.led.direction)
        self.assertFalse(self.controls.pending)
        self.assertTrue(self.controls.engine.contact["left"])
        await self.controls.advance(old_end + 10)
        self.assertIsNone(self.led.direction)
        await self.controls.command("2")
        await self.drain()
        await self.controls.command("0")
        await self.controls.advance(old_end + 30)
        self.assertIsNone(self.led.direction)
        self.assertFalse(self.transport.tasks)

    async def test_delayed_loop_does_not_flash_an_expired_automatic_round(self):
        await self.controls.command("p")
        await self.controls.command("2")
        await self.drain()
        end = self.controls.engine.active["pass_s"]
        with patch.object(practice, "time", SimpleNamespace(monotonic=lambda: end + 1)):
            await self.controls.advance(end + 1)
        self.assertIsNone(self.led.direction)
        self.assertFalse(any(r["event"] == "auto_on" for r in self.log.rows))
        self.assertIsNone(self.controls.engine.active)


class ContactEntryTest(unittest.IsolatedAsyncioTestCase):
    async def test_launcher_and_entry_skip_routes_and_video(self):
        for argv, answers in ((["practice"], []), (["practice", "p", "--simulate"], []), ([], ["practice"])):
            with self.subTest(argv=argv), patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                values = launcher.resolve_arguments(launcher.parser().parse_args(argv))
            args = parser().parse_args(values)
            self.assertTrue(args.practice)
            self.assertEqual(args.condition, "proposed" if "p" in argv else "baseline")
            with patch.object(practice, "run", new_callable=AsyncMock, return_value=0), \
                 patch("vibro.__main__.load_route") as route, patch("vibro.__main__.video.VideoPair") as video:
                self.assertEqual(await run(args), 0)
                route.assert_not_called()
                video.assert_not_called()
        for flag in ("--videos", "--validate", "--scan", "--autostart", "--allow-demo"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                await run(parser().parse_args(["--practice", flag]))
        for argv in (["practice", "--videos"], ["practice", "--validate"], ["practice", "b", "P01"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                launcher.resolve_arguments(launcher.parser().parse_args(argv))

    def test_practice_time_configuration_rejects_invalid_windows(self):
        self.assertEqual(practice.configuration({}), {"auto_on_s": 4, "pass_s": 12})
        for settings in ({"auto_on_s": 0}, {"auto_on_s": True}, {"pass_s": float("nan")},
                         {"pass_s": float("inf")}, {"pass_s": 4}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                practice.configuration({"practice": settings})

    async def test_full_session_contacts_are_logged_and_all_exit_paths_stop_outputs(self):
        for ending, expected in (("q", 0), ("eof", 0), ("fault", 2), ("cancel", 130)):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as directory:
                args = parser().parse_args(["--practice", "--simulate", "--logs", directory])
                instances = {}
                def make_transport(settings, queue, log):
                    instances["transport"] = SimTransport(settings, queue, log)
                    return instances["transport"]
                def make_led(*args):
                    instances["led"] = LedOutput(*args)
                    return instances["led"]
                def feed(loop, queue):
                    owner = asyncio.current_task()
                    queue.put_nowait(("operator", "1", time.monotonic()))
                    for delay, command in ((0.04, "left press"), (0.07, "left release"),
                                           (0.1, "left press"), (0.13, "left release")):
                        loop.call_later(delay, lambda c=command: queue.put_nowait(("operator", c, time.monotonic())))
                    if ending == "cancel":
                        loop.call_later(0.18, owner.cancel)
                    else:
                        loop.call_later(0.18, lambda: queue.put_nowait(("fault" if ending == "fault" else "operator",
                                                                      ending, time.monotonic())))
                with patch.object(practice, "SimTransport", side_effect=make_transport), \
                     patch.object(practice, "LedOutput", side_effect=make_led), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(await practice.run(args, feed), expected)
                self.assertIsNone(instances["led"].direction)
                self.assertIsNone(instances["led"].task)
                self.assertFalse(instances["transport"].tasks)
                folder = next(Path(directory).iterdir())
                with (folder / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                inputs = [r for r in rows if r["event"] == "input"]
                self.assertEqual([r["result"] for r in inputs], ["on", "recorded", "off", "recorded"])
                self.assertTrue(all(r["trial"] == "CONTACT_PRACTICE" for r in rows))
                self.assertEqual(rows[-1]["event"], "practice_closed")
                snapshot = json.loads((folder / "session.json").read_text(encoding="utf-8"))
                self.assertFalse(snapshot["route"]["scored"])
                readable = (folder / "experiment.log").read_text(encoding="utf-8")
                self.assertIn("수동 점등", readable)
                self.assertIn("수동 소등", readable)
                self.assertIn("전극 연습 종료", readable)
                self.assertNotIn("영상 기준 0초", readable)


if __name__ == "__main__":
    unittest.main()
