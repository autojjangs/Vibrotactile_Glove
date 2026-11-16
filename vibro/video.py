"""Two local mpv players with checked start, playback clocks and shutdown.

Player-reported positions are estimates, not physical display onset measurements.
Only Raspberry Pi/Linux playback is supported; rule simulations stay portable.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("navigation", "driving")
DESKTOP_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
               "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE")


def enabled(args, settings, route):
    if args.no_video:
        return False
    if args.videos:
        if not route.get("videos"):
            raise ValueError("이 시간표에는 영상이 없습니다. 경로 1 또는 2를 선택하세요.")
        return True
    return bool(settings.get("video", {}).get("enabled", False)
                and not args.simulate and not route.get("demo", False) and route.get("videos"))


def configuration(settings, route):
    c = dict(settings.get("video", {}))
    c.setdefault("media_dir", "media")
    c.setdefault("mpv", "mpv")
    # The Pi's single-video software decode test passed; test the pair the same way.
    # Keep this default in code so existing per-machine screen settings need no edit.
    c.setdefault("hwdec", "no")
    if c["hwdec"] not in ("no", "auto", "auto-copy"):
        raise ValueError("video.hwdec: use no, auto, or auto-copy")
    c.setdefault("screens", {"driving": 0, "navigation": 1})
    c.setdefault("audio", {"navigation": True, "driving": True})
    c.setdefault("prepare_timeout_s", 30.0)
    c.setdefault("command_timeout_s", 1.0)
    c.setdefault("start_timeout_s", 5.0)
    c.setdefault("poll_s", 0.1)
    c.setdefault("max_desync_s", 0.2)
    c.setdefault("max_clock_error_s", 0.2)
    c.setdefault("max_sample_rtt_s", 0.1)
    for name in ("prepare_timeout_s", "command_timeout_s", "start_timeout_s", "poll_s", "max_desync_s",
                 "max_clock_error_s", "max_sample_rtt_s"):
        value = c[name]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"video.{name} must be a finite positive number")
    if c["poll_s"] > c["max_clock_error_s"]:
        raise ValueError("video.poll_s must not exceed max_clock_error_s")
    for role in ROLES:
        if type(c["screens"][role]) is not int or not 0 <= c["screens"][role] <= 31:
            raise ValueError(f"video.screens.{role}: use a display index 0..31")
        if type(c["audio"][role]) is not bool:
            raise ValueError(f"video.audio.{role}: use true or false")
    if c["screens"]["navigation"] == c["screens"]["driving"]:
        raise ValueError("두 영상에 서로 다른 화면 번호를 지정하세요.")
    media = Path(c["media_dir"]).expanduser()
    if not media.is_absolute():
        media = ROOT / media
    paths = {}
    for role in ROLES:
        name = route.get("videos", {}).get(role)
        if not isinstance(name, str) or not name:
            raise ValueError(f"Missing route.videos.{role}")
        path = Path(name).expanduser()
        paths[role] = path if path.is_absolute() else media / path
        if not paths[role].is_file():
            raise FileNotFoundError(f"영상 파일이 없습니다: {paths[role]}")
    return c, paths


def desktop_process_options():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("영상 자동 재생은 라즈베리파이/Linux에서 실행하세요. --simulate만 쓰면 영상 없이 모의 실행합니다.")
    env = os.environ.copy()
    options = {"env": env}
    if os.geteuid() == 0:
        import pwd
        uid = int(env.get("SUDO_UID", "0"))
        if uid <= 0:
            raise RuntimeError("일반 사용자 데스크톱 터미널에서 bash run으로 실행하세요.")
        account = pwd.getpwuid(uid)
        options.update(user=uid, group=account.pw_gid,
                       extra_groups=os.getgrouplist(account.pw_name, account.pw_gid))
        env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        authority = Path(account.pw_dir) / ".Xauthority"
        if not env.get("XAUTHORITY") and authority.is_file():
            env["XAUTHORITY"] = str(authority)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        raise RuntimeError("화면 세션을 찾지 못했습니다. 라즈베리파이 데스크톱의 터미널에서 bash run을 실행하세요.")
    return options


class MpvError(RuntimeError):
    pass


@dataclass
class Sample:
    position: float
    at: float
    rtt: float
    paused: bool
    seeking: bool
    eof: bool
    speed: float

    def position_at(self, when):
        return self.position + (when - self.at if not self.paused and not self.eof else 0)


def check_samples(samples, now, origin, duration, config):
    positions = {}
    for role, sample in samples.items():
        if not math.isfinite(sample.position) or sample.position < 0:
            raise RuntimeError(f"{role}: invalid video position")
        if sample.rtt > config["max_sample_rtt_s"]:
            raise RuntimeError(f"{role}: video position response delayed ({sample.rtt:.3f}s)")
        # EOF can land one video frame before the container duration.
        ending = (sample.eof and sample.position >= duration - 0.1
                  and now - origin >= duration - 0.1)
        if sample.seeking or abs(sample.speed - 1) > 0.0001:
            raise RuntimeError(f"{role}: 영상 탐색 또는 배속 변경 감지")
        if (sample.paused or sample.eof) and not ending:
            raise RuntimeError(f"{role}: 영상 정지 또는 조기 종료 감지")
        positions[role] = sample.position_at(now)
        error = positions[role] - (now - origin)
        if abs(error) > config["max_clock_error_s"]:
            raise RuntimeError(f"{role}: 실험 시간과 영상 차이 {error:+.3f}초")
    skew = positions["driving"] - positions["navigation"]
    if abs(skew) > config["max_desync_s"]:
        raise RuntimeError(f"두 영상의 재생 시각 차이 {skew:+.3f}초")
    return dict(navigation_s=positions["navigation"], driving_s=positions["driving"],
                experiment_s=now - origin, driving_minus_navigation_s=skew,
                samples={r: vars(s) for r, s in samples.items()})


class MpvClient:
    def __init__(self, role, path, config, fault, log_path=None):
        self.role, self.path, self.config, self.fault = role, path, config, fault
        self.log_path = Path(log_path) if log_path is not None else None
        self.process = self.writer = self.reader_task = self.stderr_task = None
        self.pending = {}
        self.serial = 0
        self.closing = False
        self.stderr_tail = ""

    async def open(self):
        executable = shutil.which(self.config["mpv"])
        if not executable:
            raise RuntimeError("mpv가 없습니다. 라즈베리파이에서 sudo apt install mpv 로 설치하세요.")
        options = desktop_process_options()
        parent, child = socket.socketpair()
        command = [
            executable, "--no-config", "--load-scripts=no", "--ytdl=no",
            "--pause=yes", "--keep-open=yes", "--start=0", "--resume-playback=no",
            "--terminal=no", "--input-terminal=no", "--input-default-bindings=no",
            "--input-vo-keyboard=no", "--input-cursor=no", "--osc=no", "--osd-level=0",
            "--fullscreen=no", "--autofit=480x360", "--keepaspect=yes", "--panscan=0",
            f"--hwdec={self.config.get('hwdec', 'no')}", "--video-sync=audio", "--speed=1",
            f"--screen={self.config['screens'][self.role]}",
            f"--fs-screen={self.config['screens'][self.role]}",
            f"--title=Vibrotactile - {self.role}",
            f"--input-ipc-client=fd://{child.fileno()}",
        ]
        if self.log_path is not None:
            command.append(f"--log-file={self.log_path.resolve()}")
        if not self.config["audio"][self.role]:
            command.append("--audio=no")
        command += ["--", str(self.path.resolve())]
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command, pass_fds=(child.fileno(),), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, **options)
            child.close()
            parent.setblocking(False)
            reader, self.writer = await asyncio.open_connection(sock=parent)
            self.reader_task = asyncio.create_task(self._receive(reader))
            self.stderr_task = asyncio.create_task(self._stderr())
        except BaseException:
            parent.close()
            child.close()
            raise

    async def _stderr(self):
        while chunk := await self.process.stderr.read(1024):
            self.stderr_tail = (self.stderr_tail + chunk.decode("utf-8", "replace"))[-4096:]

    async def _receive(self, reader):
        error = None
        try:
            while line := await reader.readline():
                message = json.loads(line)
                future = self.pending.get(message.get("request_id"))
                if future is not None and not future.done():
                    if message.get("error") == "success":
                        future.set_result((message.get("data"), time.monotonic()))
                    else:
                        future.set_exception(MpvError(f"{self.role}: {message.get('error')}"))
        except asyncio.CancelledError:
            return
        except Exception as caught:
            error = caught
        finally:
            failure = MpvError(f"{self.role}: 영상 플레이어 연결 종료 ({error or self.stderr_tail[-800:]})")
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(failure)
            if not self.closing:
                self.fault(str(failure))

    async def request(self, *command):
        if self.writer is None or self.writer.is_closing():
            raise MpvError(f"{self.role}: player is not connected")
        self.serial += 1
        ref = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ref] = future
        begin = time.monotonic()
        try:
            self.writer.write((json.dumps({"command": list(command), "request_id": ref}) + "\n").encode())
            async with asyncio.timeout(self.config["command_timeout_s"]):
                await self.writer.drain()
                data, end = await future
            return data, (begin + end) / 2, end - begin
        finally:
            self.pending.pop(ref, None)

    async def get(self, name):
        return (await self.request("get_property", name))[0]

    async def set(self, name, value):
        await self.request("set_property", name, value)

    async def sample(self):
        position, at, rtt = await self.request("get_property", "time-pos")
        paused, seeking, eof, speed = await asyncio.gather(
            self.get("pause"), self.get("seeking"), self.get("eof-reached"), self.get("speed"))
        return Sample(float(position), at, rtt, paused, seeking, eof, float(speed))

    async def ready(self):
        deadline = time.monotonic() + self.config["prepare_timeout_s"]
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError(f"{self.role}: mpv 실행 실패: {self.stderr_tail[-1200:]}")
            try:
                duration, size, paused, position = await asyncio.gather(
                    self.get("duration"), self.get("video-params"), self.get("pause"), self.get("time-pos"))
                if duration and size and paused is True and position is not None:
                    if abs(float(position)) > 0.05:
                        raise RuntimeError(f"{self.role}: 영상이 0초에서 준비되지 않음")
                    return {"duration_s": duration, "video": size}
            except MpvError:
                pass
            await asyncio.sleep(0.05)
        raise RuntimeError(f"{self.role}: 영상 준비 시간 초과: {self.stderr_tail[-1200:]}")

    async def close(self):
        self.closing = True
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (OSError, ConnectionError):
                pass
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.reader_task, self.stderr_task) if t), return_exceptions=True)


class VideoPair:
    def __init__(self, settings, route, log, fault):
        self.config, self.paths = configuration(settings, route)
        self.duration, self.log, self.fault = route["duration_s"], log, fault
        directory = getattr(log, "directory", None)
        self.log_paths = {role: Path(directory) / f"mpv_{role}.log" if directory is not None else None
                          for role in ROLES}
        self.players = {role: MpvClient(role, path, self.config, fault, log_path=self.log_paths[role])
                        for role, path in self.paths.items()}
        self.monitor_task = None
        self.origin = None
        self.closing = False
        self.last_record = -float("inf")
        self.displays = None

    async def prepare(self):
        for role, player in self.players.items():
            await player.open()
            info = await player.ready()
            if float(info["duration_s"]) + 0.05 < self.duration:
                raise ValueError(f"{role}: 영상이 실험 종료 시각보다 짧습니다.")
            digest = await asyncio.to_thread(self._hash, player.path)
            self.log.write("video_file_ready", details={"role": role, "path": str(player.path),
                           "sha256": digest, "screen_index": self.config["screens"][role],
                           "audio": self.config["audio"][role], "hwdec": self.config["hwdec"],
                           "mpv_log": str(self.log_paths[role]) if self.log_paths[role] is not None else None,
                           **info})
        self.log.write("video_ready", details={"hwdec": self.config["hwdec"]})

    @staticmethod
    def _hash(path):
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    async def samples(self):
        values = await asyncio.gather(*(self.players[role].sample() for role in ROLES))
        return dict(zip(ROLES, values))

    async def check_displays(self):
        values = await asyncio.gather(*(self.players[role].get("display-names") for role in ROLES))
        if any(not isinstance(names, list) or len(names) != 1 for names in values) or set(values[0]) & set(values[1]):
            raise RuntimeError("두 영상이 서로 다른 화면에 배치되지 않았습니다. 확장 모드와 video.screens를 확인하세요.")
        mapping = dict(zip(ROLES, values))
        if self.displays is not None and mapping != self.displays:
            raise RuntimeError("재생 중 영상의 출력 화면이 변경되었습니다.")
        if self.displays is None:
            self.log.write("video_displays", details=mapping)
            self.displays = mapping

    async def start(self):
        await asyncio.gather(*(p.set("fullscreen", True) for p in self.players.values()))
        # Allow both windows to settle on their configured outputs while paused.
        await asyncio.sleep(1)
        await self.check_displays()
        before = await self.samples()
        if any(not s.paused or s.seeking or abs(s.position) > 0.05 for s in before.values()):
            raise RuntimeError("시작 전 영상이 0초·일시정지 상태가 아닙니다.")
        self.log.write("video_start_requested")
        requested = time.monotonic()
        await asyncio.gather(*(p.set("pause", False) for p in self.players.values()))
        deadline = requested + self.config["start_timeout_s"]
        while time.monotonic() < deadline:
            samples = await self.samples()
            if all(not s.paused and s.position > 0 for s in samples.values()):
                nav = samples["navigation"]
                origin = nav.at - nav.position
                if origin < requested - self.config["max_clock_error_s"]:
                    raise RuntimeError("영상 시작 시각 추정이 요청 시각보다 앞섭니다.")
                details = check_samples(samples, time.monotonic(), origin, self.duration, self.config)
                self.origin = origin
                self.log.write("video_started", details={"estimated_origin_monotonic_s": origin,
                               "request_monotonic_s": requested, **details})
                self.monitor_task = asyncio.create_task(self.monitor())
                return origin
            await asyncio.sleep(0.02)
        raise RuntimeError("두 영상의 재생 시작을 확인하지 못했습니다.")

    async def monitor(self):
        try:
            while True:
                samples = await self.samples()
                now = time.monotonic()
                details = check_samples(samples, now, self.origin, self.duration, self.config)
                if now - self.last_record >= 1:
                    await self.check_displays()
                    self.log.write("video_sync_sample", details=details)
                    self.last_record = now
                await asyncio.sleep(self.config["poll_s"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.closing:
                self.log.write("video_fault", reason=str(error))
                # Stop both video streams immediately; the experiment queue stops hardware.
                self.fault(f"영상 동기화 오류: {error}")
                await asyncio.gather(*(p.set("pause", True) for p in self.players.values()), return_exceptions=True)

    async def close(self):
        self.closing = True
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        results = await asyncio.gather(*(p.close() for p in self.players.values()), return_exceptions=True)
        errors = [str(r) for r in results if isinstance(r, BaseException)]
        if errors:
            raise RuntimeError("; ".join(errors))
        self.log.write("video_closed")
