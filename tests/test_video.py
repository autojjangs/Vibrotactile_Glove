import asyncio
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibro import video
from vibro.__main__ import parser, run
from vibro.led import LedOutput
from tests.test_experiment import MemoryLog

ROOT = Path(__file__).resolve().parents[1]
CONFIG = dict(max_sample_rtt_s=0.1, max_desync_s=0.2, max_clock_error_s=0.2)


def sample(position=2, at=102, **values):
    fields = dict(position=position, at=at, rtt=0.005, paused=False, seeking=False, eof=False, speed=1)
    return video.Sample(**(fields | values))


class VideoClockTest(unittest.TestCase):
    def test_clock_and_cross_video_alignment_use_common_time(self):
        rows = {"navigation": sample(2, 102), "driving": sample(2.04, 102.05)}
        details = video.check_samples(rows, 102.1, 100, 20, CONFIG)
        self.assertAlmostEqual(details["navigation_s"], 2.1)
        self.assertAlmostEqual(details["driving_minus_navigation_s"], -0.01)

    def test_same_stall_in_both_videos_is_detected(self):
        with self.assertRaisesRegex(RuntimeError, "실험 시간과 영상 차이"):
            video.check_samples(dict.fromkeys(video.ROLES, sample(1.6)), 102, 100, 20, CONFIG)

    def test_independent_video_skew_is_detected(self):
        rows = {"navigation": sample(1.85), "driving": sample(2.15)}
        with self.assertRaisesRegex(RuntimeError, "두 영상"):
            video.check_samples(rows, 102, 100, 20, CONFIG)

    def test_pause_seek_speed_early_end_and_delayed_read_are_detected(self):
        for fields in (dict(paused=True), dict(seeking=True), dict(speed=1.2),
                       dict(eof=True), dict(rtt=0.2), dict(position=float("nan"))):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                video.check_samples({"navigation": sample(**fields), "driving": sample()}, 102, 100, 20, CONFIG)

    def test_last_frame_at_normal_end_is_not_a_false_fault(self):
        rows = dict.fromkeys(video.ROLES, sample(19.958333, 120, paused=True, eof=True))
        video.check_samples(rows, 120, 100, 20, CONFIG)
        with self.assertRaises(RuntimeError):
            video.check_samples(rows, 120.3, 100, 20, CONFIG)


class VideoSelectionTest(unittest.TestCase):
    def test_actual_trial_enables_video_and_practice_simulation_do_not(self):
        settings = {"video": {"enabled": True}}
        route = {"videos": {"navigation": "n.mp4", "driving": "d.mp4"}, "demo": False}
        self.assertTrue(video.enabled(parser().parse_args([]), settings, route))
        self.assertFalse(video.enabled(parser().parse_args(["--simulate"]), settings, route))
        self.assertFalse(video.enabled(parser().parse_args(["--no-video"]), settings, route))
        self.assertFalse(video.enabled(parser().parse_args([]), settings, {"demo": True}))
        self.assertTrue(video.enabled(parser().parse_args(["--simulate", "--videos"]), settings, route))
        with self.assertRaises(ValueError):
            video.enabled(parser().parse_args(["--videos"]), settings, {"demo": True})

    def test_media_paths_and_configuration_fail_before_playback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("n.mp4", "d.mp4"):
                (root / name).write_bytes(b"fixture")
            route = {"videos": {"navigation": "n.mp4", "driving": "d.mp4"}}
            settings = {"video": {"media_dir": folder}}
            config, paths = video.configuration(settings, route)
            self.assertEqual(paths["navigation"], root / "n.mp4")
            self.assertEqual(config["audio"], {"navigation": True, "driving": True})
            self.assertEqual(config["hwdec"], "no")
            for mode in ("no", "auto", "auto-copy"):
                configured, _ = video.configuration({"video": settings["video"] | {"hwdec": mode}}, route)
                self.assertEqual(configured["hwdec"], mode)
            for change in ({"screens": {"navigation": 0, "driving": 0}},
                           {"poll_s": float("nan")}, {"max_desync_s": 0},
                           {"hwdec": True}, {"hwdec": "unknown"}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    video.configuration({"video": settings["video"] | change}, route)
            with self.assertRaises(FileNotFoundError):
                video.configuration(settings, {"videos": {"navigation": "missing.mp4", "driving": "d.mp4"}})


class DesktopLaunchTest(unittest.TestCase):
    def test_sudo_player_uses_original_user_and_preserves_wayland_session(self):
        from types import SimpleNamespace
        account = SimpleNamespace(pw_gid=1000, pw_name="pi", pw_dir="/home/pi")
        env = {"SUDO_UID": "1000", "WAYLAND_DISPLAY": "wayland-0",
               "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
               "HOME": "/root"}
        pwd = SimpleNamespace(getpwuid=lambda uid: account)
        with patch.object(video.sys, "platform", "linux"), \
             patch.object(video.os, "geteuid", return_value=0, create=True), \
             patch.object(video.os, "getgrouplist", return_value=[1000, 44, 109], create=True), \
             patch.dict(video.os.environ, env, clear=True), patch.dict("sys.modules", {"pwd": pwd}), \
             patch.object(Path, "is_file", return_value=False):
            options = video.desktop_process_options()
        self.assertEqual(options["user"], 1000)
        self.assertEqual(options["group"], 1000)
        self.assertEqual(options["extra_groups"], [1000, 44, 109])
        self.assertEqual(options["env"]["HOME"], "/home/pi")
        self.assertEqual(options["env"]["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(options["env"]["DBUS_SESSION_BUS_ADDRESS"], env["DBUS_SESSION_BUS_ADDRESS"])

    def test_headless_session_does_not_launch_in_an_unintended_display(self):
        with patch.object(video.sys, "platform", "linux"), \
             patch.object(video.os, "geteuid", return_value=1000, create=True), \
             patch.dict(video.os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "화면 세션"):
            video.desktop_process_options()


class FakePlayer:
    instances = []
    same_display = False

    def __init__(self, role, path, config, fault, log_path=None):
        self.role, self.path = role, path
        self.log_path = log_path
        self.paused, self.closed, self.started = True, False, None
        self.instances.append(self)

    async def open(self):
        pass

    async def ready(self):
        return {"duration_s": 20, "video": {"w": 668, "h": 668}}

    async def set(self, name, value):
        if name == "pause":
            self.paused = value
            if not value:
                self.started = time.monotonic()

    async def get(self, name):
        if name == "display-names":
            return ["HDMI-A-1" if self.role == "driving" or self.same_display else "HDMI-A-2"]
        raise AssertionError(name)

    async def sample(self):
        now = time.monotonic()
        return sample(0 if self.started is None else now - self.started,
                      now, paused=self.paused)

    async def close(self):
        self.closed = True
        self.paused = True


class VideoPairTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        for filename in ("n.mp4", "d.mp4"):
            (Path(self.temp.name) / filename).write_bytes(b"test video")
        self.settings = {"video": {"media_dir": self.temp.name}}
        self.route = {"duration_s": 20, "videos": {"navigation": "n.mp4", "driving": "d.mp4"}}
        self.log = MemoryLog()
        self.log.directory = Path(self.temp.name) / "session_logs"
        self.log.directory.mkdir()
        self.faults = []
        FakePlayer.instances = []
        FakePlayer.same_display = False

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_prepare_does_not_play_and_start_anchors_to_navigation(self):
        with patch.object(video, "MpvClient", FakePlayer):
            pair = video.VideoPair(self.settings, self.route, self.log, self.faults.append)
            try:
                await pair.prepare()
                self.assertTrue(all(p.paused for p in pair.players.values()))
                self.assertEqual({p.log_path.name for p in pair.players.values()},
                                 {"mpv_navigation.log", "mpv_driving.log"})
                self.assertTrue(all(p.log_path.parent == self.log.directory for p in pair.players.values()))
                self.assertEqual(self.log.rows[-1]["details"]["hwdec"], "no")
                origin = await pair.start()
                self.assertAlmostEqual(origin, pair.players["navigation"].started, delta=0.01)
                await asyncio.sleep(0.02)
                self.assertEqual(self.faults, [])
            finally:
                await pair.close()
        self.assertTrue(all(p.closed for p in FakePlayer.instances))
        self.assertIsNotNone(pair.players["driving"].started)
        self.assertTrue(any(r["event"] == "video_sync_sample" for r in self.log.rows))

    async def test_same_display_blocks_start_without_unpausing(self):
        FakePlayer.same_display = True
        with patch.object(video, "MpvClient", FakePlayer):
            pair = video.VideoPair(self.settings, self.route, self.log, self.faults.append)
            try:
                await pair.prepare()
                with self.assertRaisesRegex(RuntimeError, "서로 다른 화면"):
                    await pair.start()
                self.assertTrue(all(p.paused and p.started is None for p in pair.players.values()))
            finally:
                await pair.close()

    async def test_monitor_fault_pauses_both_players(self):
        with patch.object(video, "MpvClient", FakePlayer):
            pair = video.VideoPair(self.settings, self.route, self.log, self.faults.append)
            try:
                await pair.prepare()
                await pair.start()
                pair.players["driving"].started -= 1
                await asyncio.wait_for(pair.monitor_task, 1)
                self.assertEqual(len(self.faults), 1)
                self.assertTrue(all(p.paused for p in pair.players.values()))
            finally:
                await pair.close()


class MpvLaunchTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_players_use_software_decode_audio_and_distinct_diagnostic_files(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as folder:
            for role in video.ROLES:
                with self.subTest(role=role):
                    path = Path(folder) / f"{role} video.mp4"
                    log_path = Path(folder) / f"mpv_{role}.log"
                    config = {"hwdec": "no", "screens": {"driving": 0, "navigation": 1},
                              "audio": {"navigation": True, "driving": True}, "mpv": "mpv"}
                    client = video.MpvClient(role, path, config, self.fail, log_path=log_path)
                    spawn = AsyncMock(side_effect=RuntimeError("launch boundary"))
                    with patch.object(video.shutil, "which", return_value="mpv"), \
                         patch.object(video, "desktop_process_options", return_value={}), \
                         patch.object(video.asyncio, "create_subprocess_exec", spawn):
                        with self.assertRaisesRegex(RuntimeError, "launch boundary"):
                            await client.open()
                    command = spawn.call_args.args
                    self.assertIn("--hwdec=no", command)
                    self.assertFalse(any(arg.startswith("--force-render") for arg in command))
                    self.assertNotIn("--audio=no", command)
                    self.assertIn("--pause=yes", command)
                    self.assertIn("--video-sync=audio", command)
                    self.assertIn(f"--fs-screen={config['screens'][role]}", command)
                    self.assertIn(f"--log-file={log_path.resolve()}", command)
                    self.assertEqual(command[-2:], ("--", str(path.resolve())))
                    self.assertEqual(len(spawn.call_args.kwargs["pass_fds"]), 1)


class MpvIPCTest(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_event_and_out_of_order_responses_are_correlated(self):
        async def serve(reader, writer):
            first = json.loads(await reader.readline())
            second = json.loads(await reader.readline())
            writer.write(b'{"event":"property-change","id":7}\n')
            for request in (second, first):
                writer.write((json.dumps({"request_id": request["request_id"], "error": "success",
                                          "data": request["command"][-1]}) + "\n").encode())
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        client = video.MpvClient("navigation", Path("n.mp4"), {"command_timeout_s": 0.2}, self.fail)
        try:
            reader, client.writer = await asyncio.open_connection(*server.sockets[0].getsockname()[:2])
            client.reader_task = asyncio.create_task(client._receive(reader))
            self.assertEqual(await asyncio.gather(client.get("duration"), client.get("time-pos")),
                             ["duration", "time-pos"])
            self.assertFalse(client.pending)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()


class VideoRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_clock_and_cleanup_on_completion_failure_and_video_fault(self):
        for ending in ("normal", "start_failure", "fault"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                route = {
                    "route_id": "video_test", "ready": True, "demo": False, "duration_s": 0.2,
                    "videos": {"navigation": "n.mp4", "driving": "d.mp4"},
                    "events": [{"id": "turn", "direction": "left", "expected_action": "turn",
                                "preview_s": 0.08, "auto_on_s": 0.12, "pass_s": 0.18}],
                }
                route_file = root / "route.json"
                route_file.write_text(json.dumps(route), encoding="utf-8")
                args = parser().parse_args(["--simulate", "--videos", "--autostart", "--condition", "proposed",
                                           "--route", str(route_file), "--logs", str(root / "logs")])
                class FakeMedia:
                    def __init__(self, settings, route, log, fault):
                        self.fault, self.closed, self.origin = fault, False, None
                    async def prepare(self):
                        pass
                    async def start(self):
                        if ending == "start_failure":
                            raise RuntimeError("start failed")
                        self.origin = time.monotonic() - 0.04
                        if ending == "fault":
                            asyncio.get_running_loop().call_later(0.02, self.fault, "video froze")
                        return self.origin
                    async def close(self):
                        self.closed = True
                media_instances, leds = [], []
                def media_factory(*values):
                    media = FakeMedia(*values)
                    media_instances.append(media)
                    return media
                def led_factory(*values):
                    led = LedOutput(*values)
                    leds.append(led)
                    return led
                with patch.object(video, "VideoPair", side_effect=media_factory), \
                     patch("vibro.__main__.LedOutput", side_effect=led_factory), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = await run(args)
                self.assertEqual(result, 0 if ending == "normal" else 2)
                self.assertTrue(media_instances[0].closed)
                self.assertIsNone(leds[0].task)
                self.assertIsNone(leds[0].direction)
                log_file = next((root / "logs").glob("*/events.csv"))
                with log_file.open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                starts = [row for row in rows if row["event"] == "trial_start"]
                if ending == "start_failure":
                    self.assertEqual(starts, [])
                else:
                    self.assertAlmostEqual(float(starts[0]["elapsed_s"]), 0.04, delta=0.02)
                if ending == "fault":
                    self.assertFalse(any(r["event"] == "auto_on" for r in rows))


if __name__ == "__main__":
    unittest.main()
