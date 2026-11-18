"""Repeatable electrode familiarization using the actual experiment rules."""
import asyncio
import math
import sys
import time

from . import protocol as p
from .config import load_settings
from .engine import Engine
from .free_practice import FreeControls
from .led import LedOutput
from .logging import EventLog
from .transport import BleTransport, SimTransport

HELP = """숫자/문자 입력 후 Enter:
  1: 왼쪽 연습 시작       2: 오른쪽 연습 시작
  b: 수동으로 전환       p: 반자동으로 전환
  r: 같은 손으로 다시    0: 현재 회차 중단·모두 끄기
  status: 상태 확인      q: 종료       help: 사용법
수동: 사전 안내를 느낀 뒤 해당 손의 전극 접촉 → 점등, 떼었다 다시 접촉 → 소등.
반자동: 사전 안내 뒤 자동 점등 전에 접촉 → 취소, 가만히 있으면 자동 점등.
반자동은 점등 후 접촉으로 끌 수 없으며, 취소 후 재접촉해도 취소가 유지됩니다.
계속 붙이고 있으면 1회 입력입니다. 매번 떼었다 다시 접촉하세요.
회차가 끝나면 다음 1/2/r 입력을 기다립니다. 전체 연습 횟수 제한은 없습니다."""


def configuration(settings):
    config = dict(settings.get("practice", {}))
    config.setdefault("auto_on_s", 4.0)
    config.setdefault("pass_s", 12.0)
    for key in ("auto_on_s", "pass_s"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"practice.{key}: finite positive seconds required")
    if config["auto_on_s"] >= config["pass_s"]:
        raise ValueError("practice.auto_on_s must be less than practice.pass_s")
    return config


class PracticeControls:
    def __init__(self, transport, led, log, condition, config):
        self.transport, self.led, self.log = transport, led, log
        self.config = config
        self.outputs = FreeControls(transport, led, log)
        self.engine = Engine(condition, self)
        self.engine.running = True
        self.pending = []
        self.count = 0
        self.last_side = None

    def write(self, event, **fields):
        # Preserve the actual condition on rule events while the session remains
        # explicitly marked CONTACT_PRACTICE / scored=false in its metadata.
        if event == "point_pass":
            event = "practice_round_end"
        self.log.write(event, condition=self.engine.condition, **fields)

    async def apply(self, effects):
        for kind, side, value in effects:
            if kind == "led":
                await self.led.set(side, value)
            else:
                reference = await self.transport.send(side, value)
                if value == p.PREVIEW:
                    self.engine.pre_ref = reference

    async def packet(self, packet, received):
        await self.apply(self.engine.packet(packet, received))

    async def reset(self, reason):
        active = self.engine.active
        self.pending.clear()
        self.engine.active = None
        self.engine.allowed = self.engine.lit = self.engine.cancelled = False
        self.engine.pre_ref = None
        await self.outputs.stop_outputs()
        if active:
            self.write("practice_round_stopped", point=active["id"], reason=reason)

    async def begin(self, side):
        await self.reset("새 회차 시작")
        self.count += 1
        self.last_side = side
        at = time.monotonic()
        point = dict(id=f"PRACTICE_{self.count:03d}", direction=side,
                     expected_action="", preview_s=at,
                     auto_on_s=at + self.config["auto_on_s"], pass_s=at + self.config["pass_s"])
        self.write("practice_round_begin", received=at, point=point["id"], side=side,
                   details=self.config)
        if self.engine.condition == "proposed":
            self.pending.append((point["auto_on_s"], "auto", point))
        self.pending.append((point["pass_s"], "pass", point))
        await self.apply(self.engine.scheduled("preview", point, at))

    async def advance(self, until):
        # Process deadlines before an input with the same Pi receipt timestamp,
        # as in the real experiment. A very late wakeup must not flash an already
        # expired automatic indication before switching it off.
        while self.pending and self.pending[0][0] <= until:
            at, kind, point = self.pending.pop(0)
            if kind == "auto" and time.monotonic() >= point["pass_s"]:
                self.write("practice_note", details="처리가 늦어 자동 점등을 건너뜀 · 다시 연습하세요")
                continue
            await self.apply(self.engine.scheduled(kind, point, at))

    async def command(self, command):
        if command in ("1", "2"):
            await self.begin("left" if command == "1" else "right")
        elif command == "r":
            if self.last_side is None:
                self.write("practice_note", details="먼저 1(왼쪽) 또는 2(오른쪽)를 선택하세요")
            else:
                await self.begin(self.last_side)
        elif command in ("b", "p"):
            await self.reset("방식 전환")
            self.engine.condition = "baseline" if command == "b" else "proposed"
            # Contact state deliberately survives a switch: holding the cloth
            # across rounds or conditions is never counted as a fresh press.
            self.write("practice_mode")
        elif command == "0":
            await self.reset("실험자 중단")
            self.write("practice_note", details="양쪽 방향지시등·진동 정지 · 1/2/r로 다시 시작")
        elif command == "status":
            self.write("operator_status", after=self.engine.state())
        else:
            self.write("practice_note", details=f"알 수 없는 명령: {command} · 1/2/b/p/r/0/q 또는 help 사용")


async def run(args, start_reader):
    settings = load_settings(args.settings)
    config = configuration(settings)
    queue = asyncio.Queue()
    log = EventLog(args.logs, "TEST", "CONTACT_PRACTICE", "practice", "CONTACT_PRACTICE",
                   verbose=args.verbose)
    log.origin = time.monotonic()
    transport = (SimTransport if args.simulate else BleTransport)(settings, queue, log)

    def fault(reason):
        queue.put_nowait(("fault", reason, time.monotonic()))

    led = LedOutput(settings, args.simulate, log, fault)
    controls = PracticeControls(transport, led, log, args.condition, config)
    exit_code, reason = 0, "quit"
    try:
        log.snapshot(settings, {"route_id": "CONTACT_PRACTICE", "scored": False,
                               "repeatable": True, "videos": False,
                               "initial_condition": args.condition, "practice": config})
        log.write("session_open", details={"simulation": args.simulate})
        led.initialize()
        await transport.connect()
        log.write("practice_ready")
        controls.write("practice_mode")
        print(f"로그 폴더: {log.directory}\n{HELP}", flush=True)
        print(f"회차 시작 기준 {config['auto_on_s']:g}초에 반자동 점등, "
              f"{config['pass_s']:g}초에 두 방식 모두 통과·자동 소등합니다.", flush=True)
        if args.simulate:
            print("모의 접촉: left press / left release / right press / right release", flush=True)
        start_reader(asyncio.get_running_loop(), queue)
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                deadline = controls.pending[0][0] if controls.pending else None
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=max(0.001, deadline - time.monotonic())
                                                  if deadline is not None else None)
                except asyncio.TimeoutError:
                    await controls.advance(time.monotonic())
                    continue
            kind, value, received = item
            if kind == "fault":
                raise RuntimeError(value)
            # Stop commands take priority over due automatic output commands.
            command = value.strip().lower() if kind == "operator" else ""
            if kind == "operator":
                log.write("operator_input", received=received, input=value)
            if command in ("q", "quit", "exit", "abort", "eof"):
                reason = command
                break
            if command in ("0", "b", "p"):
                await controls.command(command)
                continue
            await controls.advance(received)
            if kind == "packet":
                await controls.packet(value, received)
            elif kind == "operator":
                if command in ("help", "h", "?"):
                    print(HELP, flush=True)
                elif args.simulate and command in [f"{s} {a}" for s in p.SIDES for a in ("press", "release")]:
                    side, action = command.split()
                    transport.inject(side, action == "press")
                elif command:
                    await controls.command(command)
    except asyncio.CancelledError:
        exit_code, reason = 130, "interrupted"
    except Exception as error:
        exit_code, reason = 2, str(error)
        log.write("practice_error", reason=reason)
        print(f"전극 연습 중단: {reason}", file=sys.stderr, flush=True)
    finally:
        controls.pending.clear()
        controls.engine.running = False
        controls.engine.allowed = False
        try:
            await led.stop()
        except Exception as error:
            exit_code = 2
            log.write("shutdown_led_error", reason=str(error))
        try:
            await transport.close()
        except Exception as error:
            exit_code = 2
            log.write("shutdown_ble_error", reason=str(error))
        while not queue.empty():
            kind, value, received = queue.get_nowait()
            if kind == "packet":
                await controls.packet(value, received)
            elif kind == "operator":
                log.write("operator_input", received=received, input=value,
                          result="ignored", reason="session_closing")
            elif kind == "fault":
                exit_code = 2
                log.write("shutdown_fault", received=received, reason=value)
        log.write("practice_closed", result="completed" if exit_code == 0 else "stopped", reason=reason)
        log.close()
    return exit_code
