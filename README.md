# Vibrotactile_Glove

실내 영상 기반 스마트 장갑 실험을 위한 독립 프로젝트입니다. 기존 Smart_Helmet은 변경하지 않습니다.
Python 3.11 이상, 라즈베리파이 Bluetooth LE, 좌우 ESP32-C3, 기존 WS281x 방향지시등을 사용합니다.

## 구현된 실험 규칙

| 항목 | Baseline | Proposed |
|---|---|---|
| 사전 안내 | 해당 방향 장갑에 기본 2회 진동 | 동일 |
| 입력 허용 | 첫 사전 진동의 시작 보고부터 | 동일 |
| 유효한 손 | 안내 방향의 손 | 동일 |
| 점등 전 접촉 | 점등하고 반복 진동으로 즉시 전환 | 해당 지점의 자동 점등을 취소 |
| 점등 후 접촉 | 소등 및 반복 진동 정지 | 무시 |
| 취소 후 재접촉 | 해당 없음 | 취소 유지 |
| 지점 통과 | 켜져 있으면 수동 소등 누락 기록 후 소등 | 자동 소등 |

전극을 붙이고 유지하면 1회이며, 해제 후 다시 붙여야 다음 접촉입니다. 해제와 무시된 접촉도 기록합니다.
취소한 경우 별도 성공 진동을 추가하지 않으며 남은 사전 안내는 원래대로 완료합니다.
Baseline에서 사전 안내 도중 점등하면 남은 안내를 중단하고 반복 피드백으로 바꿉니다.
첫 사전 안내 전에 이미 붙이고 있던 천은 안내가 시작되어도 새 입력으로 계산하지 않습니다.
정답 정보(expected_action)는 기록에만 쓰며 자동 점등을 막는 조건으로 사용하지 않습니다.

## 먼저 해볼 모의 실행

장갑, 라즈베리파이, 별도 Python 패키지 없이 가능합니다.

```powershell
cd D:\Vibrotactile_Glove
python -m vibro --simulate --condition baseline
```

준비 완료 후 `start`를 입력하면 T=0이 시작됩니다.
`left press`, `left release`, `right press`, `right release`로 접촉을 모의 입력합니다.
`status`는 상태 확인, `mark 내용`은 실험자 메모, `abort` 또는 Ctrl+C는 중단입니다.
한 프로세스는 한 시행을 진행합니다. 다음 시행은 새 명령으로 시작합니다.

자동 검증용 10초 예시(실제 경로 타임스탬프가 아닙니다):

```powershell
python -m vibro --simulate --autostart --condition baseline --input-script tests/demo_inputs.json
python -m vibro --simulate --autostart --condition proposed --input-script tests/demo_inputs.json
python -m unittest discover -v
```

이 입력 예시는 모든 정답 수행 예시가 아니라 조기 입력, 반대 손, 유지, 재접촉 등을 확인하는 시나리오입니다.

## 설정 파일

`config/settings.json`:
- 왼쪽 BLE MAC: 70:AF:09:34:C3:A6
- 오른쪽 BLE MAC: 70:AF:09:33:09:1E
- `haptics.preview`: 사전 안내의 on_ms, off_ms, count
- `haptics.feedback`: 점등 중 반복 진동의 on_ms, off_ms. 소등까지 무한 반복합니다.
- off_ms는 **한 진동 종료 후 다음 진동 시작까지 쉬는 시간**입니다.
- 초기 사전 안내는 200ms ON + 800ms OFF, 2회입니다. 시작 간격이 1초이며 확정된 실험 자극값은 아닙니다.
- 초기 반복 피드백은 600ms ON + 200ms OFF입니다. 두 패턴의 수치는 독립적입니다.
- LED 설정은 참고 코드의 실제 값(20개, 왼쪽 17~19, 오른쪽 0~2, GPIO18)을 옮긴 초기값입니다. 실제 배선과 대조하세요.
- `max_dispatch_lag_s`: 시간표 실행 지연 허용치(기본 0.5초). 초과하면 시행을 무효로 중단합니다.

## ESP32-C3 준비

1. Arduino IDE의 Espressif esp32 보드 패키지 **3.3.2**를 설치하고 보드는 **ESP32C3 Dev Module**, **USB CDC On Boot: Enabled**를 선택합니다.
2. `firmware/VibrotactileGlove/VibrotactileGlove.ino`를 엽니다.
3. 같은 폴더의 `config.h`에서 `CONTACT_PIN=3`으로 설정되어 있습니다. ESP32-C3 SuperMini의 GPIO3에 전극 천 A를 연결하고, GND에 천 B를 연결합니다.
4. 왼쪽 장갑은 `GLOVE_SIDE=0`, 오른쪽은 `GLOVE_SIDE=1`로 각각 업로드합니다.
5. `MOTOR_PIN=4`는 참고 코드의 초기값이므로 실제 연결과 맞추세요.
6. 전극 천 A는 GPIO3, 전극 천 B는 GND에 연결합니다. GPIO는 INPUT_PULLUP으로 읽으며 전극에 5V를 연결하지 않습니다. 5V/GND/3V3가 있는 줄을 USB 쪽부터 세면 GPIO3은 다섯 번째, GND는 두 번째입니다. 실제 보드의 3 및 G/GND 인쇄를 확인하세요.
7. 모터 핀은 기존 모터 구동 모듈의 IN에 연결합니다. 모터 자체를 GPIO에 직접 연결하지 않습니다.
8. 시리얼 모니터(115200)에서 손 구분, GPIO, BLE 주소를 확인합니다. 주소가 다르면 settings.json을 갱신합니다.

BLE UUID와 패킷이 새로 정의되어 있으므로 **두 장갑 모두 새 펌웨어**가 필요합니다.
기본 접촉 안정화 시간(DEBOUNCE_MS)은 30ms이고 config.h에서 변경할 수 있습니다.
기록되는 접촉 시각은 이 안정화 처리가 완료된 시각입니다. 원시 GPIO 바운스 하나하나는 실험 입력으로 기록하지 않습니다.

## 라즈베리파이 설치와 실행

프로젝트 전체를 라즈베리파이에 복사하고 해당 디렉터리에서:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m vibro --scan
```

기존 방향지시등의 GPIO18/PWM 배선이라면 참고 시스템에서 사용하던 오디오/PWM 설정이 필요할 수 있습니다.
기존 스마트 헬멧 프로그램과 새 실험 프로그램이 같은 LED 장치를 동시에 제어하지 않도록 하세요.

실제 실험:
```bash
sudo .venv/bin/python -m vibro --route routes/route_1.json --condition baseline --participant P01 --trial 01
sudo .venv/bin/python -m vibro --route routes/route_2.json --condition proposed --participant P01 --trial 02
```

편집 영상의 시간이 아직 없으므로 위의 실제 경로 파일은 의도적으로 실행 불가 상태입니다.
장비 연결만 예시 시간표로 점검하려면 명시적으로 `--allow-demo`를 붙입니다:
```bash
sudo .venv/bin/python -m vibro --allow-demo --condition proposed
```

## 40초 진동 비교 연습

`routes/haptic_practice_40s.json`은 10초 간격으로 4번 반복하는 장비 연습용 시간표입니다.
왼쪽·오른쪽·왼쪽·오른쪽 순서로 안내합니다. 영상 없이 사용할 수 있으며,
`start` 이후 40초에 종료됩니다. 매 회차마다 접촉하거나 가만히 있는 선택을 직접 합니다.

| 회차 | 장갑 | 사전 안내 시작 | 자동 점등 시각 | 소등·입력 마감 |
|---|---|---|---|---|
| 1 | 왼쪽 | 1초 | 3초 | 9초 |
| 2 | 오른쪽 | 11초 | 13초 | 19초 |
| 3 | 왼쪽 | 21초 | 23초 | 29초 |
| 4 | 오른쪽 | 31초 | 33초 | 39초 |

라즈베리파이에서 접촉으로 자동 점등을 취소하거나, 입력 없이 자동 점등을 관찰하려면:

```bash
sudo .venv/bin/python -m vibro --allow-demo --route routes/haptic_practice_40s.json --condition proposed --participant TEST --trial HAPTIC01
```

`READY`가 나오면 양쪽 전극 천을 떼어 놓고 `start`를 입력합니다.
첫 사전 안내 진동을 느낀 뒤 자동 점등 시각 전까지 **안내받은 손**의 천을 한 번 붙였다 떼면 그 회차의 자동 점등이 취소됩니다.
접촉하지 않으면 해당 방향지시등이 켜지고 점등 피드백 진동이 6초 동안 반복됩니다.
취소한 회차에도 남은 사전 안내 진동은 그대로 완료됩니다. 다음 회차 전에 전극 천을 떼어 주세요.
자동 점등 이후의 접촉은 취소로 처리하지 않으며, 표의 소등 시각에 자동으로 종료합니다.

현재 설정에서 사전 안내는 **0.2초 진동, 0.8초 쉼, 0.2초 진동**이며,
점등 피드백은 **0.6초 진동, 0.2초 쉼**을 소등까지 반복합니다.
강도 조절은 추가하지 않았습니다. 이 연습은 기존 펌웨어로 실행할 수 있습니다.
패턴은 현재 `config/settings.json`의 값을 사용하므로 설정을 바꾸었다면 그 값이 적용됩니다.

수동 점등·소등을 비교하려면 위 명령의 `--condition proposed`를 `--condition baseline`으로 바꿉니다.
Baseline에서는 사전 안내 이후 첫 접촉으로 켜고, 해제 후 다시 접촉하면 끕니다. 가만히 있으면 점등하지 않습니다.
반복하려면 종료 후 같은 명령을 다시 실행합니다. 중간 중단은 Ctrl+C입니다.

이 시간표는 진동 비교 연습이므로 정답·오답 평가용으로 사용하지 않습니다.
필수 필드인 `expected_action`은 모두 `turn`으로 채웠으며, 접촉 취소를 제한하지 않습니다.
프로그램의 영상 동기화 안내 문구는 실제 실험용 공통 메시지이므로 이 연습에서는 영상을 재생할 필요가 없습니다.

## 경로 타임스탬프 입력

`routes/route_1.json`, `route_2.json`에 확정된 시간을 넣고 ready를 true로 바꿉니다.
예시 형식은 demo.json을 참고하세요. 필드는 다음과 같습니다.

- route_id: 경로 식별자
- ready: 실험에 사용할 타임스탬프가 확정되었는지
- demo: 실제 경로는 false
- duration_s: 영상 기준 시행 종료 시각
- events: 안내 지점들의 배열
- 각 지점: id, direction(left/right), expected_action(turn/straight), preview_s, auto_on_s, pass_s, cue_s(직진 단서)
- 시간은 모두 영상 시작 기준 초 단위 소수입니다.
- 이전 지점 통과 < 다음 사전 안내 < 자동 점등 < 지점 통과 순서여야 합니다.
- 직진 단서는 첫 사전 안내 이전 또는 같은 시각이어야 합니다. ‘처음부터 켜지 않음’이라는 정답 기준을 위한 제약입니다.
- Baseline에서도 auto_on_s를 기록하지만 그 시각에 자동 점등하지 않습니다.

`python -m vibro --route routes/route_1.json --validate`로 입력값을 확인합니다.

**영상 자동 재생/동기화는 포함하지 않습니다.** start 명령의 T=0과 두 영상의 재생 기준을 실험 운영에서 맞춰야 합니다.
현재 버전은 중간 일시정지·탐색을 지원하지 않습니다. 영상이 멈추거나 시간을 이동하면 시행을 중단하고 처음부터 다시 시작하세요.
video_cue_marker는 시간표상 단서 시각이며 실제 화면 표시를 측정한 값이 아닙니다.

## 로그

기본 터미널 출력은 **한국어 실험 로그**입니다. 경과시간, 좌우, 안내 지점과 함께
사전 안내, 접촉·해제, 수동 점등·소등, 자동 점등·취소, 지점 통과, 오류를 표시합니다.
무시된 접촉도 사유를 표시하며, 천을 떼는 동작은 상태를 바꾸지 않는 입력으로 기록합니다.
반복되는 통신 확인, 명령 응답, 패킷 번호와 같은 내부 처리 기록은 기본 화면에서 숨깁니다.

Proposed에서 취소한 예시(설명용 시각):
```text
[   5.010초] [안내] [demo_straight] 오른쪽 사전 안내 시작 · 자동 점등 취소 가능
[   5.400초] [입력] [demo_straight] 오른쪽 전극 접촉 → 자동 점등 취소
[   5.600초] [입력] [demo_straight] 오른쪽 전극 떼기 → 기록 · 상태 유지
[   7.000초] [취소] [demo_straight] 오른쪽 취소 유지 → 자동 점등하지 않음
```

`status`도 상태를 한국어로 출력합니다. 통신 문제를 살펴볼 때는 실행 명령 끝에 `--verbose`를 붙이면
숨긴 상세 이벤트도 터미널에 추가로 표시합니다. 실험 동작과 저장되는 원본 데이터는 같으며,
펌웨어를 다시 업로드할 필요가 없습니다.

`logs/UTC시각_참가자_시행_조건/`마다 다음을 저장합니다.
- experiment.log: 기본 화면과 같은 읽기 쉬운 한국어 실험 이벤트 기록(UTF-8, 즉시 저장)
- events.csv: 상세 이벤트를 포함한 전체 원본 로그(UTF-8 BOM, 즉시 저장)
- session.json: 해당 시행의 실험 정보, 설정값, 경로 시간표 사본

CSV의 열과 기존 이벤트 이름은 유지합니다. CSV에는 UTC 기록 시각, 영상 기준 경과시간,
Pi 단조 시각, 참가자 코드, 시행·조건·경로·지점, 손, 접촉/해제, 처리 결과와 무시 사유,
상태 전후, ESP 기동 후 시간, 패킷 순번, 명령 번호가 들어갑니다.
연결 준비 중, 반대 손, 사전 안내 전, 취소 후, 자동 점등 후의 입력도 기록합니다.
패킷 송수신, 장갑에서 적용된 진동 모드, LED 드라이버 명령, 시간표 지연,
실험자 입력/메모, 통신 오류도 CSV에 그대로 기록합니다.

- input: 안정화된 접촉·해제와 처리 결과
- contact_snapshot: 연결 준비 상태 확인. 새 접촉으로 계산하지 않습니다.
- haptic_applied: 장갑이 GPIO 제어를 적용했다고 보낸 보고. 물리 진동 센서로 측정한 시각은 아닙니다.
- led_driver_on/off: LED 드라이버 호출 결과. 실제 발광 센서 측정값은 아닙니다.
- manual_off_missed: Baseline에서 지점을 통과할 때 아직 켜져 있음을 기록합니다.
- result=ignored는 정답/오답 판정과 다릅니다. 전체 연구 지표 계산은 확정된 분석 기준으로 별도 수행하세요.
- 기본 화면의 점등·소등 문구는 제어 판단을 표시합니다. 실제 발광 여부는 장치에서 확인하세요.
- 화면과 experiment.log는 경과시간을 소수 셋째 자리까지 표시하며, CSV에는 기존 정밀도를 유지합니다.
- ESP device_ms와 Pi 시각은 서로 다른 시계입니다. 두 값을 직접 빼서 무선 지연으로 해석하지 마세요.

## 오류 및 경계 처리

- 두 장갑 연결과 알림 구독, 초기 상태 조회 후 시작합니다.
- 입력 수용 시작은 첫 진동 시작 보고 수신 시점입니다. 무선 전달 시간만큼 실제 모터 시작과 차이가 날 수 있으며 두 시각을 모두 기록합니다.
- 같은 Pi 수신 시각에 입력과 자동 점등/통과 기한이 겹치면 시간표 이벤트를 먼저 처리합니다.
- 연결 해제, 패킷 순번 누락, 잘못된 좌우 펌웨어, 명령 적용 응답 누락, LED 오류는 해당 시행을 무효로 중단합니다.
- 중단 후 LED와 양쪽 진동을 정지합니다. ESP 자체도 연결 해제 또는 4초 동안 명령이 없으면 진동을 멈춥니다.
- 누락된 무선 입력을 복원할 수 있다고 가정하지 않습니다. 자동으로 재접속해 같은 시행을 계속하지 않고, 연결을 복구한 뒤 새 시행으로 시작합니다.

## 연구 운영

각 참가자는 두 경로를 서로 다른 조건으로 1회씩 수행합니다.
참가자 간 조건 순서뿐 아니라 경로-조건 배정도 교차하세요. 이 프로그램은 배정표를 자동 생성하지 않으며 실행 인자로 지정한 값을 기록합니다.
참가자 코드에는 이름 대신 P01 같은 연구용 식별자를 사용합니다.

## 참고한 공식 문서

- [Bleak client API](https://bleak.readthedocs.io/en/stable/api/client.html)
- [Espressif BLE](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/ble.html)
- [Espressif GPIO](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/gpio.html)

[SuperMini 핀 배치 자료](https://dl.artronshop.co.th/ESP32-C3%20SuperMini%20datasheet.pdf)

## 현재 검증 상태

Python 테스트 23개와 모의 시행 3개를 통과했습니다. 펌웨어 전체 빌드는 Windows 앱 제어 정책의 컴파일러 실행 차단으로 미완료입니다. 자세한 결과와 실제 장비 확인 항목은 [VALIDATION.md](VALIDATION.md)를 참고하세요.
