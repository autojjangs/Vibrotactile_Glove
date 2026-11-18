import argparse
import asyncio
import re
import sys
import threading
import time
from pathlib import Path
from . import protocol as p
from .config import load_settings, load_route, timeline, read_json
from .engine import Engine
from .logging import EventLog
from .transport import BleTransport, SimTransport
from .led import LedOutput
from . import video

ROOT = Path(__file__).resolve().parent.parent

def parser():
    ap = argparse.ArgumentParser(description="Vibrotactile Glove experiment / 양방향 장갑 실험")
    ap.add_argument("--settings", type=Path, default=ROOT / "config/settings.json")
    ap.add_argument("--route", type=Path, default=ROOT / "routes/demo.json")
    ap.add_argument("--condition", choices=["baseline", "proposed"], default="baseline")
    ap.add_argument("--participant", default="PILOT")
    ap.add_argument("--trial", default="01")
    ap.add_argument("--simulate", action="store_true", help="no BLE or LED hardware")
    practice = ap.add_mutually_exclusive_group()
    practice.add_argument("--free", action="store_true", help="시간 제한 없는 진동·방향지시등 자유 체험")
    practice.add_argument("--practice", action="store_true", help="반복 가능한 전극·진동·방향지시등 조작 연습")
    ap.add_argument("--practice-window", action="store_true", help="전극 연습을 큰 글씨·버튼 창으로 표시")
    ap.add_argument("--allow-demo", action="store_true", help="explicitly allow synthetic timeline on hardware for bench checks")
    ap.add_argument("--autostart", action="store_true", help="simulation only")
    ap.add_argument("--input-script", type=Path, help="simulation only; timed contact/release JSON")
    ap.add_argument("--logs", type=Path, default=ROOT / "logs")
    ap.add_argument("--validate", action="store_true", help="validate settings and timeline then exit")
    ap.add_argument("--scan", action="store_true", help="scan BLE devices and exit")
    ap.add_argument("--verbose", action="store_true", help="터미널에 통신 등 상세 로그도 표시")
    media = ap.add_mutually_exclusive_group()
    media.add_argument("--videos", action="store_true", help="영상 자동 재생 강제 사용 (모의 장비 실행에서도 사용)")
    media.add_argument("--no-video", action="store_true", help="영상 없이 실행 (장비 점검용)")
    return ap

def reader(loop, queue):
    # Daemon thread: a blocked stdin must never prevent emergency shutdown.
    def work():
        while True:
            line = sys.stdin.readline()
            if not line:
                line = "eof"
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("operator", line.strip(), time.monotonic()))
            except RuntimeError:
                return
            if line == "eof":
                return
    threading.Thread(target=work, daemon=True).start()

def load_input_script(path):
    values = read_json(path) if path else []
    last = -1
    for value in values:
        t = value.get("at_s")
        if isinstance(t, bool) or not isinstance(t, (float, int)) or not 0 <= t < float("inf") or t < last:
            raise ValueError("Input script times must be finite and sorted")
        if value.get("side") not in p.SIDES or value.get("action") not in ("press", "release"):
            raise ValueError("Input script requires side=left/right and action=press/release")
        last = t
    return values

async def run(args):
    if args.practice_window and not args.practice:
        raise ValueError("--practice-window requires --practice")
    if args.practice:
        if args.scan or args.validate or args.autostart or args.input_script or args.allow_demo or args.videos:
            raise ValueError("--practice cannot be combined with scan/validate/autostart/input-script/allow-demo/videos")
        from .contact_practice import run as practice_run
        return await practice_run(args, reader)
    if args.free:
        if args.scan or args.validate or args.autostart or args.input_script or args.allow_demo or args.videos:
            raise ValueError("--free cannot be combined with scan/validate/autostart/input-script/allow-demo")
        from .free_practice import run as free_run
        return await free_run(args, reader)
    settings, route = load_settings(args.settings), load_route(args.route)
    if args.validate:
        print(f"VALID: {route['route_id']}, {len(route['events'])} points, {route['duration_s']} seconds")
        return 0
    if route.get("demo", False) and not (args.simulate or args.allow_demo):
        raise ValueError("Synthetic demo timeline: use --simulate or explicitly --allow-demo for a hardware bench test")
    if not args.simulate and (args.autostart or args.input_script):
        raise ValueError("--autostart / --input-script are for simulation only")
    for value in (args.participant, args.trial):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
            raise ValueError("Participant/trial: use 1..40 letters, numbers, _ or -; do not enter real names")
    use_video = video.enabled(args, settings, route)
    inputs = load_input_script(args.input_script)
    queue = asyncio.Queue()
    log = EventLog(args.logs, args.participant, args.trial, args.condition, route["route_id"], verbose=args.verbose)
    log.snapshot(settings, route)
    engine = Engine(args.condition, log)
    transport = (SimTransport if args.simulate else BleTransport)(settings, queue, log)
    def fault(reason):
        queue.put_nowait(("fault", reason, time.monotonic()))
    led = LedOutput(settings, args.simulate, log, fault)
    schedule = timeline(route)
    index = 0
    script_task = None
    media = None
    exit_code = 0
    async def apply(effects):
        for kind, side, value in effects:
            if kind == "led":
                await led.set(side, value)
            else:
                ref = await transport.send(side, value)
                if value == p.PREVIEW:
                    engine.pre_ref = ref
    async def advance(until):
        nonlocal index
        if log.origin is None or not engine.running:
            return
        while index < len(schedule) and log.origin + schedule[index][0] <= until and engine.running:
            seconds, _, kind, point = schedule[index]
            now = time.monotonic()
            lag = max(0, now - (log.origin + seconds))
            log.write("timeline_event", details={"kind": kind, "scheduled_s": seconds, "dispatch_lag_s": lag})
            if lag > settings["max_dispatch_lag_s"]:
                raise RuntimeError(f"Timeline delayed by {lag:.3f}s; trial invalid")
            await apply(engine.scheduled(kind, point, now))
            index += 1
    async def playback():
        for value in inputs:
            await asyncio.sleep(max(0, log.origin + value["at_s"] - time.monotonic()))
            transport.inject(value["side"], value["action"] == "press")
    async def start():
        nonlocal script_task
        if log.origin is not None:
            log.write("operator_ignored", reason="already_started")
            return
        log.origin = await media.start() if media else time.monotonic()
        engine.start()
        if media:
            print("영상 재생 확인 · 내비 영상의 0초를 기준으로 실험을 시작했습니다.", flush=True)
        else:
            print("영상 두 개의 재생 기준을 지금의 0초에 맞춰주세요.", flush=True)
        if inputs:
            script_task = asyncio.create_task(playback())

    try:
        log.write("session_open", details={"simulation": args.simulate, "demo": route.get("demo", False),
                                          "video_playback": use_video})
        if use_video:
            media = video.VideoPair(settings, route, log, fault)
            await media.prepare()
        led.initialize()
        await transport.connect()
        print(f"로그 폴더: {log.directory}\n명령: start 시작 | status 상태 | mark 내용 메모 | abort 중단 | quit 종료", flush=True)
        log.write("session_ready")
        if args.simulate:
            print("모의 전극 입력: left press / left release / right press / right release", flush=True)
        if args.autostart:
            await start()
        else:
            reader(asyncio.get_running_loop(), queue)
        while log.origin is None or engine.running:
            deadline = log.origin + schedule[index][0] if log.origin is not None and index < len(schedule) else None
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=max(0.001, deadline - time.monotonic()) if deadline else None)
                except asyncio.TimeoutError:
                    await advance(time.monotonic())
                    continue
            kind, value, received = item
            # Process events in Pi receive-time order. At a deadline tie, timeline wins.
            if kind == "fault":
                raise RuntimeError(value)
            await advance(received)
            if kind == "packet":
                await apply(engine.packet(value, received))
            elif kind == "operator":
                log.write("operator_input", received=received, input=value)
                if value == "start":
                    await start()
                elif value in ("abort", "quit", "eof"):
                    engine.abort(value)
                    exit_code = 2 if log.origin is not None else 0
                    break
                elif value == "status":
                    log.write("operator_status", after=engine.state())
                elif value.startswith("mark "):
                    log.write("operator_marker", details=value[5:])
                elif args.simulate and value in [f"{s} {a}" for s in p.SIDES for a in ("press", "release")]:
                    side, action = value.split()
                    transport.inject(side, action == "press")
                else:
                    log.write("operator_ignored", input=value, reason="unknown_command")
    except asyncio.CancelledError:
        engine.abort("interrupted")
        exit_code = 2
    except Exception as error:
        engine.abort(str(error))
        print(f"실험 중단: {error}", file=sys.stderr, flush=True)
        exit_code = 2
    finally:
        media_close = asyncio.create_task(media.close()) if media else None
        if script_task:
            script_task.cancel()
            await asyncio.gather(script_task, return_exceptions=True)
        try:
            await led.stop()
        except Exception as error:
            log.write("shutdown_led_error", reason=str(error))
        try:
            await transport.close()
        except Exception as error:
            log.write("shutdown_ble_error", reason=str(error))
        if media_close:
            try:
                await media_close
            except Exception as error:
                log.write("video_fault", reason=f"영상 종료 오류: {error}")
                exit_code = 2
        # Keep all final received input and stop reports in the same log.
        while not queue.empty():
            kind, value, received = queue.get_nowait()
            if kind == "packet":
                engine.packet(value, received)
            elif kind == "fault":
                log.write("shutdown_fault", received=received, reason=value)
                exit_code = 2
        log.write("session_closed", result="completed" if exit_code == 0 and log.origin is not None else "not_completed")
        log.close()
    return exit_code

async def scan():
    from bleak import BleakScanner
    for device in await BleakScanner.discover(timeout=8):
        print(device.address, device.name or "(unnamed)")
    return 0

def main():
    args = parser().parse_args()
    try:
        if (args.free or args.practice or args.practice_window) and args.scan:
            raise ValueError("--free / --practice cannot be combined with --scan")
        return asyncio.run(scan() if args.scan else run(args))
    except (ValueError, KeyError, OSError, ImportError) as error:
        print(f"Configuration/setup error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
