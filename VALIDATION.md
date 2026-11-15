# 검증 기록

검증일: 2026-11-15 (Asia/Seoul)

## 통과

- Python 3.13.15에서 단위/비동기 테스트 23개 통과.
- Baseline 10초 모의 시행: 접촉·해제, 조기 입력, 반대 손, 접촉 유지, 점등·소등, 종료 확인.
- Proposed 10초 모의 시행: 취소, 취소 유지, 재접촉 무시, 종료 확인.
- Proposed 무입력 10초 모의 시행: 자동 점등 2회, 지점 통과 소등 2회, 정상 종료 확인.
- 마지막 모의 시행은 D:\Vibrotactile_Glove에서 실행하고 해당 폴더에 로그를 저장.
- 새 프로젝트의 좌우 MAC 주소, GPIO3 설정, 미확정 실제 경로 실행 차단 확인.

## 펌웨어 빌드 제한

Espressif Arduino core 3.3.2 / ESP32C3 Dev Module / USB CDC On Boot enabled 기준으로 빌드를 시도했다.
한글 경로 문제를 해결하고 도구를 프로젝트의 .arduino 폴더로 옮겼으나,
Windows Application Control이 riscv32-esp-elf-gcc.exe 실행을 차단했다.

최종 오류: "An Application Control policy has blocked this file."

따라서 전체 펌웨어 빌드/링크는 검증 완료로 표시하지 않는다.
build 폴더에 일부 중간 산출물이 있어도 업로드 가능한 검증 완료 펌웨어로 간주하지 않는다.
validation/firmware-build-blocked.txt에 실제 오류 로그가 있다.
Windows 보안 설정은 변경하지 않았다.

## 아직 필요한 실제 장비 확인

- GPIO3(전극 A), GND(전극 B), GPIO4(기본 모터 모듈 IN) 배선 확인.
- 좌우 GLOVE_SIDE를 각각 0/1로 설정한 펌웨어의 전체 빌드 및 업로드.
- 실제 Raspberry Pi와 장갑 두 개의 BLE 연결, 접촉 감지, 진동/LED 동작, 연결 해제 시 정지 확인.
- 완성된 영상 타임스탬프를 route_1.json / route_2.json에 입력.
- 영상 두 개와 프로그램 T=0 동기화 및 실제 지연 측정.

## 예시 로그

validation/baseline-demo.csv, proposed-demo.csv, automatic-demo.csv는 합성 시간표로 생성한 검증 자료다.
참가자 실험 데이터가 아니다. demo.json과 tests/demo_inputs.json이 해당 시나리오다.
실제 연구 데이터는 logs/의 시행별 events.csv 및 session.json에 저장된다.

validation/ 폴더의 생성 로그와 오류 출력은 로컬 검증 자료이므로 Git 업로드에서 제외합니다. 이 검증 요약 문서와 tests/의 테스트 코드·입력 예시는 저장소에 포함됩니다.

