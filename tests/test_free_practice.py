import asyncio
import contextlib
import csv
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vibro import free_practice, launcher, protocol as p
from vibro.__main__ import parser, run
from vibro.config import load_settings
from vibro.led import LedOutput
from vibro.transport import SimTransport
from tests.test_experiment import MemoryLog

ROOT = Path(__file__).resolve().parents[1]


class FreeControlsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = load_settings(ROOT / "config/settings.json")
        self.settings["haptics"]["preview"] = {"on_ms": 1, "off_ms": 1, "count": 2}
        self.queue, self.log = asyncio.Queue(), MemoryLog()
        self.transport = SimTransport(self.settings, self.queue, self.log)
        self.led = LedOutput(self.settings, True, self.log, self.fail)
        self.controls = free_practice.FreeControls(self.transport, self.led, self.log)

    async def asyncTearDown(self):
        await self.led.stop()
        await self.transport.close()

    def drain_packets(self):
        while not self.queue.empty():
            kind, packet, received = self.queue.get_nowait()
            if kind == "packet":
                self.controls.packet(packet, received)

    async def test_preview_finishes_without_lighting_led_and_can_repeat(self):
        for side, key in (("left", "1"), ("right", "2"), ("left", "1")):
            await self.controls.command(key)
            await asyncio.sleep(0.02)
            self.drain_packets()
            self.assertIsNone(self.led.direction)
            self.assertIsNone(self.controls.preview_reference)
            self.assertEqual(self.log.rows[-1]["event"], "free_preview_finished")
            self.assertEqual(self.log.rows[-1]["side"], side)

    async def test_feedback_persists_until_off_and_contact_does_not_toggle(self):
        await self.controls.command("3")
        self.transport.inject("left", True)
        self.transport.inject("left", True)
        self.transport.inject("left", False)
        self.drain_packets()
        await asyncio.sleep(0.03)
        self.assertEqual(self.led.direction, "left")
        self.assertFalse(self.led.task.done())
        contacts = [row for row in self.log.rows if row["event"] == "input"]
        self.assertEqual([row["input"] for row in contacts], ["contact", "release"])
        self.assertTrue(all(row["result"] == "recorded" for row in contacts))
        await self.controls.command("0")
        self.assertIsNone(self.led.direction)
        self.assertIsNone(self.led.task)
        stops = [row for row in self.log.rows if row["event"] == "sim_command"][-2:]
        self.assertEqual([(r["side"], r["details"]["opcode"]) for r in stops],
                         [("left", p.STOP), ("right", p.STOP)])

    async def test_switching_side_or_preview_stops_previous_feedback(self):
        for key, direction in (("3", "left"), ("4", "right"), ("1", None), ("3", "left")):
            await self.controls.command(key)
            self.assertEqual(self.led.direction, direction)
            commands = [row for row in self.log.rows if row["event"] == "sim_command"][-3:]
            self.assertEqual([row["details"]["opcode"] for row in commands[:2]], [p.STOP, p.STOP])
        await asyncio.sleep(0.02)
        self.drain_packets()
        self.assertEqual(self.led.direction, "left")
        # The old preview completion cannot stop the newly selected feedback.
        self.assertIsNone(self.controls.preview_reference)

    async def test_stop_failure_still_attempts_other_glove_and_blocks_new_action(self):
        async def send(side, mode):
            if side == "left":
                raise RuntimeError("disconnected")
        transport = type("Transport", (), {})()
        transport.send = AsyncMock(side_effect=send)
        led = type("Led", (), {})()
        led.stop, led.set = AsyncMock(), AsyncMock()
        controls = free_practice.FreeControls(transport, led, self.log)
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            await controls.command("4")
        self.assertEqual([call.args for call in transport.send.await_args_list],
                         [("left", p.STOP), ("right", p.STOP)])
        led.stop.assert_awaited_once()
        led.set.assert_not_awaited()


class FreeSessionTest(unittest.IsolatedAsyncioTestCase):
    async def session(self, ending):
        with tempfile.TemporaryDirectory() as directory:
            args = parser().parse_args(["--free", "--simulate", "--logs", directory])
            instances = {}
            def transport_factory(settings, queue, log):
                transport = SimTransport(settings, queue, log)
                instances["transport"] = transport
                return transport
            def led_factory(*args):
                led = LedOutput(*args)
                instances["led"] = led
                return led
            def feed(loop, queue):
                queue.put_nowait(("operator", "3", time.monotonic()))
                queue.put_nowait(("operator", "left press", time.monotonic()))
                queue.put_nowait(("operator", "left release", time.monotonic()))
                if ending == "cancel":
                    loop.call_soon(asyncio.current_task().cancel)
                elif ending == "fault":
                    queue.put_nowait(("fault", "test disconnect", time.monotonic()))
                else:
                    queue.put_nowait(("operator", ending, time.monotonic()))
            with patch.object(free_practice, "SimTransport", side_effect=transport_factory), \
                 patch.object(free_practice, "LedOutput", side_effect=led_factory), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
                result = await free_practice.run(args, feed)
            self.assertIsNone(instances["led"].direction)
            self.assertIsNone(instances["led"].task)
            self.assertFalse(instances["transport"].tasks)
            folder = next(Path(directory).iterdir())
            with (folder / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["input"] for r in rows if r["event"] == "input"], ["contact", "release"])
            self.assertTrue(all(r["condition"] == "free" for r in rows))
            self.assertEqual(rows[-1]["event"], "free_closed")
            snapshot = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["metadata"]["trial"], "FREE")
            self.assertFalse(snapshot["route"]["timed"])
            self.assertFalse(snapshot["route"]["scored"])
            readable = (folder / "experiment.log").read_text(encoding="utf-8")
            self.assertIn("전극 접촉", readable)
            self.assertIn("방향지시등 점등", readable)
            self.assertNotIn("start 입력으로 시작", stdout.getvalue())
            return result

    async def test_quit_and_eof_stop_outputs_and_keep_final_contact_logs(self):
        for ending in ("q", "eof"):
            with self.subTest(ending=ending):
                self.assertEqual(await self.session(ending), 0)

    async def test_fault_and_keyboard_cancellation_stop_outputs(self):
        self.assertEqual(await self.session("fault"), 2)
        self.assertEqual(await self.session("cancel"), 130)

    async def test_free_entry_skips_timeline_and_rejects_conflicting_options(self):
        args = parser().parse_args(["--free", "--simulate"])
        with patch("vibro.__main__.load_route") as route, \
             patch.object(free_practice, "run", new_callable=AsyncMock, return_value=0) as free:
            self.assertEqual(await run(args), 0)
            free.assert_awaited_once()
            route.assert_not_called()
        for flag in ("--validate", "--autostart", "--allow-demo", "--scan"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                await run(parser().parse_args(["--free", flag]))


class FreeLauncherTest(unittest.TestCase):
    def test_shortcut_and_menu_launch_free_without_trial_prompts(self):
        for argv, answers in ((["free"], []), ([], ["free"]),
                              (["free", "--simulate", "--verbose"], [])):
            with self.subTest(argv=argv), patch("builtins.input", side_effect=answers):
                values = launcher.resolve_arguments(launcher.parser().parse_args(argv))
            args = parser().parse_args(values)
            self.assertTrue(args.free)
            self.assertEqual(args.simulate, "--simulate" in argv)
            self.assertEqual(args.verbose, "--verbose" in argv)
            self.assertNotIn("--route", values)
            with patch.object(launcher.sys, "platform", "linux"), \
                 patch.object(launcher.os, "geteuid", return_value=1000, create=True):
                command = launcher.execution_command(values)
            self.assertEqual(command[0] == "sudo", "--simulate" not in argv)

    def test_condition_or_validation_cannot_silently_change_free_mode(self):
        for argv in (["free", "b"], ["free", "--validate"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                launcher.resolve_arguments(launcher.parser().parse_args(argv))


if __name__ == "__main__":
    unittest.main()
