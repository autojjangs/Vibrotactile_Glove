import argparse
import asyncio
import json
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

ROOT = Path(__file__).resolve().parent.parent

def parser():
    ap = argparse.ArgumentParser(description="Vibrotactile Glove experiment / 양방향 장갑 실험")
    ap.add_argument("--settings", type=Path, default=ROOT / "config/settings.json")
    ap.add_argument("--route", type=Path, default=ROOT / "routes/demo.json")
    ap.add_argument("--condition", choices=["baseline", "proposed"], default="baseline")
    ap.add_argument("--participant", default="PILOT")
    ap.add_argument("--trial", default="01")
    ap.add_argument("--simulate", action="store_true", help="no BLE or LED hardware")
    ap.add_argument("--allow-demo", action="store_true", help="explicitly allow synthetic timeline on hardware for bench checks")
    ap.add_argument("--autostart", action="store_true", help="simulation only")
    ap.add_argument("--input-script", type=Path, help="simulation only; timed contact/release JSON")
    ap.add_argument("--logs", type=Path, default=ROOT / "logs")
    ap.add_argument("--validate", action="store_true", help="validate settings and timeline then exit")
    ap.add_argument("--scan", action="store_true", help="scan BLE devices and exit")
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
    inputs = load_input_script(args.input_script)
    queue = asyncio.Queue()
    log = EventLog(args.logs, args.participant, args.trial, args.condition, route["route_id"])
    log.snapshot(settings, route)
    engine = Engine(args.condition, log)
    transport = (SimTransport if args.simulate else BleTransport)(settings, queue, log)
    def fault(reason):
        queue.put_nowait(("fault", reason, time.monotonic()))
    led = LedOutput(settings, args.simulate, log, fault)
    schedule = timeline(route)
    index = 0
    script_task = None
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
    def start():
        nonlocal script_task
        if log.origin is not None:
            log.write("operator_ignored", reason="already_started")
            return
        log.origin = time.monotonic()
        engine.start()
        print("T=0 START - start both synchronized videos now.", flush=True)
        if inputs:
            script_task = asyncio.create_task(playback())

    try:
        log.write("session_open", details={"simulation": args.simulate, "demo": route.get("demo", False)})
        led.initialize()
        await transport.connect()
        print(f"Logs: {log.directory}\nREADY. Commands: start | status | mark TEXT | abort | quit", flush=True)
        if args.simulate:
            print("Simulation contact commands: left press / left release / right press / right release", flush=True)
        if args.autostart:
            start()
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
            await advance(received)
            if kind == "fault":
                raise RuntimeError(value)
            if kind == "packet":
                await apply(engine.packet(value, received))
            elif kind == "operator":
                log.write("operator_input", received=received, input=value)
                if value == "start":
                    start()
                elif value in ("abort", "quit", "eof"):
                    engine.abort(value)
                    exit_code = 2 if log.origin is not None else 0
                    break
                elif value == "status":
                    print(json.dumps(engine.state(), ensure_ascii=False), flush=True)
                elif value.startswith("mark "):
                    log.write("operator_marker", details=value[5:])
                elif args.simulate and value in [f"{s} {a}" for s in p.SIDES for a in ("press", "release")]:
                    side, action = value.split()
                    transport.inject(side, action == "press")
                else:
                    log.write("operator_ignored", reason="unknown_command")
    except asyncio.CancelledError:
        engine.abort("interrupted")
        exit_code = 2
    except Exception as error:
        engine.abort(str(error))
        print(f"STOPPED: {error}", file=sys.stderr, flush=True)
        exit_code = 2
    finally:
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
        return asyncio.run(scan() if args.scan else run(args))
    except (ValueError, KeyError, OSError, ImportError) as error:
        print(f"Configuration/setup error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
