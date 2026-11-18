import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from vibro import launcher, practice_window as window, protocol as p
from vibro.__main__ import parser
from vibro.logging import format_experiment_event


class WindowViewTest(unittest.TestCase):
    def state(self, **values):
        return dict(condition="baseline", active=False, side="left", allowed=True,
                    lit=False, cancelled=False, auto_on_s=4, pass_s=12, elapsed_s=0) | values

    def test_instructions_track_actual_manual_and_proposed_state(self):
        self.assertIn("선택", window.presentation(self.state())["title"])
        self.assertIn("맞대", window.presentation(self.state(active=True))["title"])
        manual = window.presentation(self.state(active=True, lit=True))
        self.assertIn("다시 맞대도 켜진 상태를 유지", manual["instruction"])
        self.assertIn("통과 시 자동", manual["instruction"])
        cancel = window.presentation(self.state(active=True, condition="proposed", cancelled=True))
        self.assertIn("취소했어요", cancel["title"])
        lit = window.presentation(self.state(active=True, condition="proposed", lit=True))
        self.assertIn("취소할 수 없어요", lit["instruction"])
        pending = window.presentation(self.state(active=True, condition="proposed", elapsed_s=2))
        self.assertIn("자동 점등까지 2.0초", pending["time_text"])

    def test_launcher_defaults_to_window_and_terminal_is_explicit(self):
        for extra, expected in (([], True), (["--terminal"], False)):
            args = parser().parse_args(launcher.resolve_arguments(launcher.parser().parse_args(["practice"] + extra)))
            self.assertEqual(args.practice_window, expected)
        with self.assertRaises(ValueError):
            launcher.resolve_arguments(launcher.parser().parse_args(["1b", "--terminal"]))

    def test_sync_messages_are_readable_and_do_not_claim_realignment(self):
        base = dict(event="", side="", direction="", condition="baseline", elapsed_s=2, point="")
        line = format_experiment_event(base | dict(event="video_sync_warning", reason="내비 차이 -0.3초", details={"grace_s": 2}))
        self.assertIn("계속 진행", line)
        line = format_experiment_event(base | dict(event="video_sync_recovered", details={"duration_s": 0.4}))
        self.assertIn("회복", line)
        self.assertIn("0.40초", line)


class WindowPipeTest(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_user_launch_commands_and_close_use_private_pipes(self):
        queue = asyncio.Queue()
        ui = window.PracticeWindow(queue)
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdin = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock())
        process = SimpleNamespace(stdin=stdin, stdout=stdout, stderr=stderr,
                                  wait=AsyncMock(return_value=0), terminate=Mock())
        stdout.feed_data(b'{"command":"ready"}\n{"command":"1"}\n')
        options = {"env": {"WAYLAND_DISPLAY": "wayland-0"}, "user": 1000, "group": 1000}
        with patch.object(window.sys, "platform", "linux"), \
             patch.object(window, "desktop_process_options", return_value=options), \
             patch.object(window.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            await ui.open()
            try:
                self.assertEqual(spawn.call_args.kwargs["user"], 1000)
                self.assertIn("vibro.practice_window", spawn.call_args.args)
                self.assertEqual((await queue.get())[:2], ("operator", "1"))
                await ui.publish({"message": "전극 접촉"})
                self.assertEqual(json.loads(stdin.write.call_args.args[0]), {"message": "전극 접촉"})
                stdout.feed_data(b'{"command":"q"}\n')
                self.assertEqual((await asyncio.wait_for(queue.get(), 1))[:2], ("operator", "q"))
            finally:
                await ui.close()
        stdin.close.assert_called_once()


