"""Short commands and an interactive launcher for the Raspberry Pi."""
import argparse
import os
from pathlib import Path
from .video import DESKTOP_ENV
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
ROUTES = {"1": "route_1.json", "2": "route_2.json", "test": "haptic_practice_40s.json"}
ASSIGNMENTS = {
    "1b": ("1", "baseline"),
    "1p": ("1", "proposed"),
    "2b": ("2", "baseline"),
    "2p": ("2", "proposed"),
}
ROUTE_CHOICES = (*ROUTES, *ASSIGNMENTS, "free", "practice")
ASSIGNMENT_MENU = """본 실험 배정 조건:
  1b: 경로 1 · 수동 (baseline)
  1p: 경로 1 · 반자동 (proposed)
  2b: 경로 2 · 수동 (baseline)
  2p: 경로 2 · 반자동 (proposed)
연습: practice = 전극 조작 반복 연습 / test = 40초 연습 / free = 진동 자유 체험"""
CONDITIONS = {"b": "baseline", "baseline": "baseline", "p": "proposed", "proposed": "proposed"}


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise argparse.ArgumentTypeError("영문·숫자·_·-로 1~40자를 입력하세요. 예: P01, 01")
    return value


def parser():
    ap = argparse.ArgumentParser(
        prog="bash run",
        description="장갑 실험 실행: 생략한 항목은 실행 후 입력합니다.",
        epilog="예: bash run | bash run 1p | bash run practice | bash run practice p | bash run test | bash run free",
    )
    ap.add_argument("route", nargs="?", type=str.lower, choices=ROUTE_CHOICES,
                    help="배정: 1b / 1p / 2b / 2p, 경로만 선택: 1 / 2, 연습: practice / test / free")
    ap.add_argument("condition", nargs="?", type=str.lower, choices=CONDITIONS,
                    help="b: 수동, p: 자동 점등·취소")
    ap.add_argument("participant", nargs="?", type=identifier, help="참가자 코드 (예: P01)")
    ap.add_argument("trial", nargs="?", type=identifier, help="시행 번호 (예: 01)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--simulate", action="store_true", help="장비 없이 모의 실행")
    mode.add_argument("--validate", action="store_true", help="시간표와 설정만 검사")
    mode.add_argument("--scan", action="store_true", help="BLE 장갑 검색만 실행")
    ap.add_argument("--verbose", action="store_true", help="상세 통신 로그 표시")
    ap.add_argument("--terminal", action="store_true", help="practice를 별도 창 없이 터미널에서 사용")
    media = ap.add_mutually_exclusive_group()
    media.add_argument("--videos", action="store_true", help="모의 실행에서도 실제 영상 표시")
    media.add_argument("--no-video", action="store_true", help="영상 없이 장비만 점검")
    return ap


def ask_choice(prompt, choices):
    while True:
        value = input(prompt).strip().lower()
        if value in choices:
            return value
        print("다음 중 하나를 입력하세요: " + " / ".join(choices))


def ask_identifier(prompt, default=None):
    while True:
        value = input(prompt).strip()
        if not value and default is not None:
            value = default
        try:
            return identifier(value)
        except argparse.ArgumentTypeError as error:
            print(error)


def resolve_arguments(args):
    if args.terminal and (args.scan or args.route not in (None, "practice")):
        raise ValueError("--terminal은 practice에서만 사용하세요.")
    if args.scan:
        return ["--scan"]

    route = args.route
    if route is None:
        if args.validate:
            route = ask_choice("검사할 경로 [1 / 2]: ", ("1", "2"))
        else:
            print(ASSIGNMENT_MENU, flush=True)
            route = ask_choice("배정 조건 또는 연습 선택: ", (*ASSIGNMENTS, "practice", "test", "free"))
    condition = args.condition
    if args.terminal and route != "practice":
        raise ValueError("--terminal은 practice에서만 사용하세요.")
    if route in ASSIGNMENTS:
        if condition is not None:
            raise ValueError("배정 코드에 방식이 포함되어 있습니다. 예: bash run 1p (방식 생략)")
        route, condition = ASSIGNMENTS[route]
    if route == "free":
        if args.validate or args.videos or any((args.condition, args.participant, args.trial)):
            raise ValueError("free는 방식·참가자·시행 또는 --validate 없이 사용하세요. 예: bash run free")
        return ["--free"] + [flag for flag in ("--simulate", "--verbose")
                             if getattr(args, flag[2:])]
    if route == "practice":
        if args.validate or args.videos or args.participant or args.trial:
            raise ValueError("practice는 본 실험 정보·영상 없이 사용하세요. 예: bash run practice 또는 bash run practice p")
        return ["--practice", "--condition", CONDITIONS[condition or "b"]] + ([] if args.terminal else ["--practice-window"]) + [
            flag for flag in ("--simulate", "--verbose") if getattr(args, flag[2:])]
    result = ["--route", str(ROOT / "routes" / ROUTES[route])]
    if args.validate:
        return result + ["--validate"]

    practice = route == "test"
    condition = condition or (
        "p" if practice else ask_choice("방식 [b: 수동 / p: 반자동(자동 점등·취소)]: ", CONDITIONS)
    )
    condition = CONDITIONS[condition]
    participant = args.participant or (
        "TEST" if practice else ask_identifier("참가자 코드 (예: P01): ")
    )
    trial = args.trial or (
        "PRACTICE" if practice else ask_identifier("시행 번호 [Enter: 01]: ", default="01")
    )
    result += ["--condition", condition, "--participant", participant, "--trial", trial]
    if practice:
        result.append("--allow-demo")
    if args.simulate:
        result.append("--simulate")
    if args.verbose:
        result.append("--verbose")
    if args.videos:
        result.append("--videos")
    if args.no_video:
        result.append("--no-video")

    route_label = "40초 연습 (좌→우→좌→우)" if practice else f"경로 {route}"
    condition_label = "수동 (baseline)" if condition == "baseline" else "반자동 · 자동 점등/취소 (proposed)"
    print(f"\n{route_label} | {condition_label} | 참가자 {participant} | 시행 {trial}", flush=True)
    if args.simulate:
        print("장비 없이 모의 실행합니다.", flush=True)
    print("연결 준비가 완료되면 start를 입력하세요. 중단: Ctrl+C\n", flush=True)
    return result


def execution_command(arguments):
    command = [sys.executable, "-m", "vibro", *arguments]
    hardware = not any(flag in arguments for flag in ("--scan", "--validate", "--simulate"))
    # GPIO18/PWM requires root on the experiment's Raspberry Pi 4.
    if hardware and sys.platform.startswith("linux") and os.geteuid() != 0:
        command = ["sudo", "--preserve-env=" + ",".join(DESKTOP_ENV), *command]
    return command


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    if args.scan and any((args.route, args.condition, args.participant, args.trial)):
        ap.error("--scan은 경로·방식·참가자·시행 없이 사용하세요.")
    try:
        arguments = resolve_arguments(args)
        command = execution_command(arguments)
        os.chdir(ROOT)
        # Replace the launcher so Ctrl+C reaches the existing shutdown handling.
        os.execvp(command[0], command)
    except ValueError as error:
        ap.error(str(error))
    except (EOFError, KeyboardInterrupt):
        print("\n실행을 취소했습니다.", flush=True)
        return 130
    except OSError as error:
        print(f"실행할 수 없습니다: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
