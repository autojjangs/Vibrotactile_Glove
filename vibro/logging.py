import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import protocol as p

FIELDS = ("utc_time", "elapsed_s", "received_monotonic_s", "participant", "trial",
          "condition", "route", "event", "side", "point", "direction",
          "expected_action", "input", "result", "reason", "before", "after",
          "device_ms", "sequence", "reference", "details")

SIDE_NAMES = {"left": "왼쪽", "right": "오른쪽"}
CONDITION_NAMES = {"baseline": "Baseline · 수동 점등", "proposed": "Proposed · 자동 점등/취소",
                   "free": "자유 체험 (시간 제한 없음)"}
INPUT_REASONS = {
    "duplicate_state": "이미 같은 접촉 상태여서 새 입력이 아님",
    "trial_not_running": "실험 진행 중이 아님",
    "outside_point": "현재 입력 가능한 안내 지점이 아님",
    "before_first_vibration": "첫 사전 안내 진동이 시작되기 전",
    "wrong_hand": "안내 방향과 다른 손",
    "already_cancelled": "이미 취소됨 · 취소 상태 유지",
    "after_automatic_on": "이미 자동 점등됨 · 점등 후에는 취소 불가",
}
ERROR_EVENTS = {
    "ble_invalid_packet": "장갑에서 잘못된 통신 데이터 수신",
    "shutdown_command_failed": "종료 시 장갑 진동 정지 명령 실패",
    "disconnect_failed": "장갑 연결 종료 실패",
    "shutdown_led_error": "종료 시 방향지시등 정지 오류",
    "shutdown_ble_error": "종료 시 장갑 통신 오류",
    "shutdown_fault": "종료 처리 중 오류",
}
# Only known diagnostic events are hidden. New or unrecognized events stay visible.
DETAIL_EVENTS = {
    "ble_rx", "ble_command", "sim_rx", "sim_command", "sim_hold",
    "timeline_event", "preview_requested", "led_driver_on", "led_driver_off",
    "operator_input", "video_sync_sample", "video_file_ready",
}


def format_status(state, condition):
    if not state.get("running"):
        phase, window = "대기/종료", "불가"
    elif not state.get("point"):
        phase, window = "진행 중", "안내 지점 대기"
    elif not state.get("allowed"):
        phase, window = "진행 중", "첫 사전 안내 진동 대기"
    elif condition == "proposed" and state.get("cancelled"):
        phase, window = "진행 중", "취소 완료"
    elif condition == "proposed" and state.get("lit"):
        phase, window = "진행 중", "자동 점등 후 취소 불가"
    else:
        phase, window = "진행 중", "수동 점등/소등 가능" if condition == "baseline" else "자동 점등 취소 가능"
    light = "켜짐" if state.get("lit") else "꺼짐"
    return f"{phase} | 지점: {state.get('point') or '-'} | 방향지시등: {light} | 입력: {window}"


def format_experiment_event(row):
    """Return one readable line, or None for routine diagnostic traffic.

    Values here are the original typed fields; CSV serialization happens later.
    """
    event = row["event"]
    side = SIDE_NAMES.get(row.get("side") or row.get("direction"), "")
    hand = f"{side} " if side else ""
    reason = row.get("reason", "")
    details = row.get("details") or {}
    category = "실험"

    if event in DETAIL_EVENTS:
        return None
    if event == "input":
        category = "입력"
        action = "전극 접촉" if row.get("input") == "contact" else "전극 떼기"
        result = row.get("result")
        outcomes = {
            "on": "수동 점등",
            "off": "수동 소등 · 진동 정지",
            "cancel": "자동 점등 취소",
            "recorded": "기록 · 상태 유지",
        }
        outcome = outcomes.get(result, str(result or "결과 없음"))
        if result == "ignored":
            outcome = "무시: " + INPUT_REASONS.get(reason, reason or "사유 없음")
        message = f"{hand}{action} → {outcome}"
    elif event == "input_window_open":
        category = "안내"
        action = "수동 점등/소등 가능" if row["condition"] == "baseline" else "자동 점등 취소 가능"
        message = f"{hand}사전 안내 시작 · {action}"
    elif event == "auto_on":
        category, message = "점등", f"{hand}방향지시등 자동 점등"
    elif event == "auto_suppressed":
        category, message = "취소", f"{hand}취소 유지 → 자동 점등하지 않음"
    elif event == "haptic_applied":
        if details.get("mode") == p.PREVIEW and row["condition"] == "free":
            category, message = "진동", f"{hand}사전 안내 진동 시작"
        elif details.get("mode") == p.FEEDBACK:
            category, message = "진동", f"{hand}점등 피드백 진동 시작"
        else:
            return None
    elif event == "free_ready":
        category, message = "체험", "준비 완료 (READY) · 1/2 사전 안내 · 3/4 점등 · 0 끄기 · q 종료"
    elif event == "free_indicator_on":
        category, message = "체험", f"{hand}방향지시등 점등 · 직접 끌 때까지 유지"
    elif event == "free_outputs_off":
        category, message = "체험", "양쪽 방향지시등·진동 정지"
    elif event == "free_preview_finished":
        category, message = "체험", f"{hand}사전 안내 종료 · 다시 선택하면 반복"
    elif event == "free_unknown_command":
        category, message = "명령", f"알 수 없는 명령: {row.get('input', '')} · 1/2/3/4/0/q 또는 help 사용"
    elif event == "free_error":
        category, message = "오류", f"자유 체험 중단 · {reason}"
    elif event == "free_closed":
        category, message = "종료", "자유 체험 종료 · 로그 저장 완료"
        if row.get("result") != "completed":
            message += f" · 중단 사유: {reason}"
    elif event == "point_pass":
        category = "통과"
        if (row.get("before") or {}).get("lit"):
            message = f"{hand}안내 지점 통과 → 자동 소등 · 진동 정지"
        else:
            message = f"{hand}안내 지점 통과 · 입력 구간 종료"
    elif event == "video_ready":
        mode = details.get("hwdec")
        description = {"no": "소프트웨어 재생 (가속 끔)", "auto": "하드웨어 가속 자동 선택",
                       "auto-copy": "하드웨어 가속 자동 선택 (복사 방식)"}.get(mode)
        playback = f" · {description}" if description else ""
        if details.get("gpu_api") == "opengl":
            playback += " · OpenGL 화면 출력"
        category, message = "영상", f"두 영상 준비 완료{playback} · 영상창이 앞에 있으면 Alt+Tab으로 터미널에 돌아와 start 입력"
    elif event == "video_displays":
        category, message = "영상", f"출력 화면 확인 · 주행 {details.get('driving')} / 내비 {details.get('navigation')}"
    elif event == "video_start_requested":
        category, message = "영상", "두 영상 재생 시작 요청"
    elif event == "video_started":
        category, message = "영상", f"재생 시작 확인 · 주행−내비 재생 시각 차이 {details.get('driving_minus_navigation_s', 0):+.3f}초"
    elif event == "video_fault":
        category, message = "오류", f"영상 재생 중단 · {reason}"
    elif event == "video_closed":
        category, message = "영상", "두 영상 종료"
    elif event == "video_cue_marker":
        category, message = "영상", "직진 단서 예정 시각 (시간표 기준)"
    elif event == "session_open":
        category = "준비"
        condition = CONDITION_NAMES.get(row["condition"], row["condition"])
        mode = "모의 실행" if details.get("simulation") else "장비 실행"
        if details.get("demo"):
            mode += " · 데모 시간표"
        if details.get("video_playback"):
            mode += " · 영상 자동 재생"
        message = (f"{condition} | 참가자 {row['participant']} | 시행 {row['trial']} | "
                   f"경로 {row['route']} | {mode}")
    elif event == "ble_scan_start":
        category, message = "연결", "양쪽 장갑 검색 중"
    elif event in ("ble_connected", "sim_connected"):
        category = "연결"
        message = f"{hand}장갑 " + ("모의 연결 완료" if event == "sim_connected" else "연결 완료")
    elif event == "ble_disconnected":
        category, message = "연결", f"{hand}장갑 연결 해제"
    elif event == "contact_snapshot":
        if not row.get("input"):
            return None
        category, message = "확인", f"{hand}전극이 이미 닿아 있음 · 떼었다 다시 접촉해야 새 입력"
    elif event == "session_ready":
        category, message = "준비", "양쪽 장갑 준비 완료 (READY) · start 입력으로 시작"
    elif event == "trial_start":
        category, message = "시작", "실험 시작 · 영상 기준 0초"
    elif event == "trial_complete":
        category, message = "종료", "시행 시간 종료"
    elif event == "trial_aborted":
        category = "중단"
        reasons = {
            "abort": "실험자가 abort 입력",
            "quit": "실험자가 quit 입력",
            "eof": "명령 입력 연결 종료",
            "interrupted": "키보드 중단 (Ctrl+C)",
        }
        message = "시행 중단 · " + reasons.get(reason, reason or "원인 정보 없음")
        if (row.get("before") or {}).get("running"):
            message += " · 분석에서 제외할 시행"
    elif event == "session_closed":
        category = "종료"
        message = "정상 종료 · 로그 저장 완료" if row.get("result") == "completed" else "로그 저장 완료 · 시행 미완료"
    elif event == "operator_marker":
        category, message = "메모", str(row.get("details", ""))
    elif event == "operator_status":
        category, message = "상태", format_status(row["after"], row["condition"])
    elif event == "operator_ignored":
        category = "명령"
        if reason == "already_started":
            message = "이미 시작한 시행입니다"
        else:
            message = f"알 수 없는 명령: {row.get('input', '')} · start / status / mark 내용 / abort / quit 사용"
    elif event in ERROR_EVENTS:
        category, message = "오류", f"{hand}{ERROR_EVENTS[event]}: {reason or '원인 정보 없음'}"
    else:
        category = "알림"
        extra = reason or row.get("details") or row.get("result")
        message = f"{hand}{event}" + (f" · {extra}" if extra else "")

    elapsed = row.get("elapsed_s", "")
    clock = f"{float(elapsed):8.3f}초" if elapsed != "" else "준비 중"
    point = f" [{row['point']}]" if row.get("point") else ""
    # A marker or error should not split one event over several console lines.
    message = message.replace("\r", r"\r").replace("\n", r"\n")
    return f"[{clock}] [{category}]{point} {message}"


def sudo_log_owner():
    """Keep newly created logs writable by the user who invoked sudo."""
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        return None
    try:
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        return None
    return (uid, gid) if uid > 0 and gid >= 0 else None


class EventLog:
    def __init__(self, directory, participant, trial, condition, route, echo=True, verbose=False):
        root = Path(directory)
        self.owner = sudo_log_owner()
        root_created = not root.exists()
        root.mkdir(parents=True, exist_ok=True)
        if root_created:
            self._set_owner(root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.directory = root / f"{stamp}_{participant}_{trial}_{condition}"
        try:
            self.directory.mkdir()
        except PermissionError as error:
            raise PermissionError(error.errno, f"로그 저장 폴더에 쓸 권한이 없습니다. 소유권을 확인하세요: {root}",
                                  str(self.directory)) from error
        self._set_owner(self.directory)
        self.handle = self.readable_handle = None
        try:
            self.handle = (self.directory / "events.csv").open("x", encoding="utf-8-sig", newline="")
            self.readable_handle = (self.directory / "experiment.log").open("x", encoding="utf-8", newline="")
            if self.owner:
                for handle in (self.handle, self.readable_handle):
                    os.fchown(handle.fileno(), *self.owner)
        except BaseException:
            for handle in (self.handle, self.readable_handle):
                if handle:
                    handle.close()
            raise
        self.writer = csv.DictWriter(self.handle, FIELDS)
        self.writer.writeheader()
        self.handle.flush()
        self.context = dict(participant=participant, trial=trial, condition=condition, route=route)
        self.origin = None
        self.echo = echo
        self.verbose = verbose

    def _set_owner(self, path):
        if self.owner:
            os.chown(path, *self.owner, follow_symlinks=False)

    def write(self, event, received=None, **fields):
        now = time.monotonic() if received is None else received
        row = dict.fromkeys(FIELDS, "")
        row.update(self.context, utc_time=datetime.now(timezone.utc).isoformat(),
                   received_monotonic_s=f"{now:.6f}", event=event)
        if self.origin is not None:
            row["elapsed_s"] = f"{now - self.origin:.6f}"
        for key, value in fields.items():
            if key not in row:
                raise ValueError(f"Unknown log field: {key}")
            row[key] = value
        csv_row = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                   for key, value in row.items()}
        self.writer.writerow(csv_row)
        self.handle.flush()
        readable = format_experiment_event(row)
        if readable is not None:
            self.readable_handle.write(readable + "\n")
            self.readable_handle.flush()
            if self.echo:
                print(readable, flush=True)
        elif self.echo and self.verbose:
            print(f"[{row['elapsed_s'] or 'READY':>10}] [상세] {event} "
                  f"{row['side']} {row['point']} {row['input']} {row['result']} "
                  f"{row['reason']} {csv_row['details']}", flush=True)

    def snapshot(self, settings, route):
        (self.directory / "session.json").write_text(
            json.dumps({"metadata": self.context, "settings": settings, "route": route},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        self._set_owner(self.directory / "session.json")

    def close(self):
        self.readable_handle.close()
        self.handle.close()
