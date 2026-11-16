"""Untimed, operator-controlled familiarization using the existing firmware."""
import asyncio
import sys
import time

from . import protocol as p
from .config import load_settings
from .led import LedOutput
from .logging import EventLog
from .transport import BleTransport, SimTransport

ACTIONS = {
    "1": ("left", p.PREVIEW),
    "2": ("right", p.PREVIEW),
    "3": ("left", p.FEEDBACK),
    "4": ("right", p.FEEDBACK),
}
HELP = """숫자 입력 후 Enter:
  1: 왼쪽 사전 안내 진동     2: 오른쪽 사전 안내 진동
  3: 왼쪽 방향지시등+진동   4: 오른쪽 방향지시등+진동
  0: 모두 끄기             q: 종료
  help: 사용법 다시 보기
새 동작을 고르면 이전 동작은 정지합니다.
사전 안내는 한 패턴 재생 후 멈추고, 점등은 0 또는 다른 동작을 고를 때까지 유지합니다.
전극 접촉·해제는 기록만 합니다. 시간 제한과 영상은 없습니다."""


class FreeControls:
    def __init__(self, transport, led, log):
        self.transport, self.led, self.log = transport, led, log
        self.preview_reference = None

    async def stop_outputs(self):
        self.preview_reference = None
        errors = []
        try:
            await self.led.stop()
        except Exception as error:
            errors.append(f"LED: {error}")
        # Attempt both gloves even if one side cannot acknowledge STOP.
        for side in p.SIDES:
            try:
                await self.transport.send(side, p.STOP)
            except Exception as error:
                errors.append(f"{side}: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))

    async def command(self, value):
        if value == "0":
            await self.stop_outputs()
            self.log.write("free_outputs_off")
            return
        side, mode = ACTIONS[value]
        await self.stop_outputs()
        if mode == p.FEEDBACK:
            await self.led.set(side, True)
        reference = await self.transport.send(side, mode)
        if mode == p.PREVIEW:
            self.preview_reference = (side, reference)
        else:
            self.log.write("free_indicator_on", side=side)

    def packet(self, packet, received):
        fields = dict(received=received, side=packet.side, device_ms=packet.device_ms,
                      sequence=packet.sequence, reference=packet.reference)
        if packet.kind == p.CONTACT:
            self.log.write("input", **fields, input="contact" if packet.value else "release",
                           result="recorded", reason="free_mode_no_contact_action")
        elif packet.kind == p.STATE:
            self.log.write("contact_snapshot", **fields, input=bool(packet.value))
        elif packet.kind == p.HAPTIC:
            self.log.write("haptic_applied", **fields, details={"mode": packet.value})
            if (packet.side, packet.reference) == self.preview_reference and packet.value == p.STOP:
                self.preview_reference = None
                self.log.write("free_preview_finished", **fields)


async def run(args, start_reader):
    settings = load_settings(args.settings)
    queue = asyncio.Queue()
    log = EventLog(args.logs, "TEST", "FREE", "free", "FREE_EXPERIENCE", verbose=args.verbose)
    log.origin = time.monotonic()
    transport = (SimTransport if args.simulate else BleTransport)(settings, queue, log)

    def fault(reason):
        queue.put_nowait(("fault", reason, time.monotonic()))

    led = LedOutput(settings, args.simulate, log, fault)
    controls = FreeControls(transport, led, log)
    exit_code = 0
    reason = "quit"
    try:
        log.snapshot(settings, {"route_id": "FREE_EXPERIENCE", "timed": False, "scored": False})
        log.write("session_open", details={"simulation": args.simulate})
        led.initialize()
        await transport.connect()
        log.write("free_ready")
        print(f"로그 폴더: {log.directory}\n{HELP}", flush=True)
        if args.simulate:
            print("모의 접촉: left press / left release / right press / right release", flush=True)
        start_reader(asyncio.get_running_loop(), queue)
        while True:
            kind, value, received = await queue.get()
            if kind == "fault":
                raise RuntimeError(value)
            if kind == "packet":
                controls.packet(value, received)
                continue
            if kind != "operator":
                continue
            log.write("operator_input", received=received, input=value)
            command = value.strip().lower()
            if command in ("q", "quit", "exit", "abort", "eof"):
                reason = command
                break
            if command in ACTIONS or command == "0":
                await controls.command(command)
            elif command in ("help", "h", "?"):
                print(HELP, flush=True)
            elif args.simulate and command in [f"{s} {a}" for s in p.SIDES for a in ("press", "release")]:
                side, action = command.split()
                transport.inject(side, action == "press")
            elif command:
                log.write("free_unknown_command", input=value)
    except asyncio.CancelledError:
        exit_code, reason = 130, "interrupted"
    except Exception as error:
        exit_code, reason = 2, str(error)
        log.write("free_error", reason=reason)
        print(f"자유 체험 중단: {error}", file=sys.stderr, flush=True)
    finally:
        try:
            await led.stop()
        except Exception as error:
            exit_code = 2
            log.write("shutdown_led_error", reason=str(error))
        try:
            # Existing close stops both motors, heartbeat and BLE connections.
            await transport.close()
        except Exception as error:
            exit_code = 2
            log.write("shutdown_ble_error", reason=str(error))
        while not queue.empty():
            kind, value, received = queue.get_nowait()
            if kind == "packet":
                controls.packet(value, received)
            elif kind == "operator":
                log.write("operator_input", received=received, input=value,
                          result="ignored", reason="session_closing")
            elif kind == "fault":
                exit_code = 2
                log.write("shutdown_fault", received=received, reason=value)
        log.write("free_closed", result="completed" if exit_code == 0 else "stopped", reason=reason)
        log.close()
    return exit_code
