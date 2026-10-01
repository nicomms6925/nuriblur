# 10. 로드맵 · 작업 체크리스트

PR 제목에 ID를 붙인다. 각 항목은 완료 기준이 있어야 체크한다.

## G0 뼈대 (1주)
- [x] G0-01 저장소 구조·pyproject·pre-commit·ruff·mypy
- [x] G0-02 `make proto` (grpcio-tools) → `worker/pb`, `app/pb` — `scripts/gen_proto.py` (make 없는 환경도 동작)
- [x] G0-03 `models/manifest.json` 스키마 + `models/registry.py`(해시·라이선스 검증, EP 선택)
- [x] G0-04 CI: lint·pytest·`check_license.py` — `.github/workflows/ci.yml` (windows-latest)
- [x] G0-05 `tests/test_exposure_audit.py` 뼈대(실패 상태), `scripts/bench.py` 뼈대 — G1에서 통과 상태로 전환
- [x] G0-06 회귀 세트 폴더 규약(`tests/data/{genre}/{clip}.mp4` + GT json, git-lfs) — `tests/data/README.md`, `scripts/make_test_clips.py`(합성 클립+GT)

## G1 워커 CLI (3주)
- [x] G1-01 probe (ffprobe, VFR·회전) — ffprobe 대신 PyAV
- [x] G1-02 decode (PyAV HW 디코드, 링버퍼, SW 폴백 이벤트) — HW 디코드는 GPU EP 있을 때만 시도
- [x] G1-03 detect (ONNX 배치, 검출 간격, 공개 얼굴 모델 + 번호판 1차 모델) — YuNet(얼굴)·YOLOX COCO(전신·차량)·RT-DETRv2 번호판(Open Images, Apache-2.0) + 못 찾은 차량은 하단 폴백. 적응형 검출 간격(빠른 움직임 시 매 프레임)
- [x] G1-04 ByteTrack 포팅 + 칼만 보간 + 패딩 — 안전 편차: 저신뢰 검출도 트랙 생성(docs/11 §3)
- [x] G1-05 link(얼굴↔전신), 병합 제안 — 병합 제안은 외형 특징(ArcFace 전 임시)
- [x] G1-06 .nbproj 읽기/쓰기, checkpoint 재개
- [x] G1-07 rules.apply (deny-by-default, REVIEW 플래그)
- [x] G1-08 mask (픽셀화·블러·단색, 하한, EMA, 20px 폴백) — EMA∪원박스, 얼굴 없는 전신 머리 마스킹
- [x] G1-09 encode (NVENC/QSV/openh264, 오디오 copy, 메타 제거) — NVENC→QSV→AMF→MediaFoundation→openh264→mpeg4, GPL 인코더 금지. 이 PC는 MF만 검증
- [x] G1-10 audit_check + CLI 종료 코드 — 2패스 합의 + 무결성 검사(docs/11 §3)
- [x] G1-11 gRPC 서버·토큰·Control(pause/resume/cancel)
- [x] G1-12 CLI `probe/analyze/rules/render/audit`
- [x] G1-13 포맷 매트릭스 테스트(코덱×컨테이너×VFR×회전) — mp4/mkv/mov × h264/mpeg4 × 회전 × VFR × 메타 제거
- [ ] G1-14 bench: RTX 4060 1080p 분석 ≥60 FPS 보고 — 부분: bench·GPU 모니터(nvidia-smi) 구현. **GPU 측정은 GPU PC에서 필요**(개발 PC GPU 없음). CPU 결과는 docs/11 §4
- **게이트**: 회귀 세트에서 노출 재검사 0건, 얼굴 누락률 <3% — 합성 회귀 클립 통과(얼굴·번호판 누락률 0%, 노출 0). 실영상(Mixkit 7편, `scripts/regress.py`)은 군중 타임랩스에서 미통과 — 아래 열린 질문

## G2 UI 1~5단계 (3주)
- [x] G2-01 메인 윈도우 레이아웃·테마·토큰·i18n
- [x] G2-02 상태 머신·워커 프로세스 관리(시작/재기동/재개)
- [x] G2-03 1단계 입력·큐·프로파일
- [x] G2-04 2단계 모니터(이벤트 구독·게이지·로그·미리보기·제어)
- [x] G2-05 3단계 캔버스 박스 오버레이·트랙 목록·토글
- [x] G2-06 타임라인 레인·플레이헤드·스크러빙(GetFrame)
- [x] G2-07 4단계 검수(병합·수동 박스·전/후 비교)
- [x] G2-08 5단계 내보내기·완료 처리
- [x] G2-09 pytest-qt 시나리오(취소·재개·크래시 복구)

## G3 기관 모드 (2주) → MVP 완료
- [x] G3-01 org.sqlite·결재선 설정 UI·스냅샷
- [x] G3-02 결재 흐름(승인/반려/동적 단계)·권한
- [x] G3-03 감사 로그(해시 체인·트리거·내보내기)
- [x] G3-04 보고서 PDF(한글 폰트)·워터마크·해시·보관 기간 삭제 스케줄
- [x] G3-05 폐쇄망 설치본(PyInstaller+Inno Setup, 모델 번들) — `scripts/build/build_release.ps1`로 LGPL PyAV 교체·라이선스 게이트 후 온라인/폐쇄망 설치 파일 빌드 확인(각 247MB). 빌드된 exe의 워커 모드로 분석·렌더·재검사 검증
- [ ] G3-06 기관 PoC 1곳 실행·피드백 반영

## G4 V1 (6주)
- [ ] G4-01 번호판 2차 파인튜닝(현장 2,000장)·OCR 한글 사전·정규식·REVIEW — 부분: 1차 범용 번호판 모델 연결, 번호판 재검사(글자 획 판별), 합성 국내 번호판 생성기(`scripts/data/synth_plates.py`). 현장 데이터 파인튜닝·OCR은 데이터 필요
- [ ] G4-02 ArcFace 자체 학습·MatchReference·참조 사진 UI
- [ ] G4-03 번호판 텍스트·영역 제외·구간 제외 규칙 — 부분: 워커 규칙(plate_text·region·timerange) 구현·테스트. UI 그리기는 미구현
- [x] G4-04 검수 고도화(누락 의심 자동 목록, 키프레임 보간 UX) — 누락 의심·노출 목록, 2키프레임 수동 박스, **마스킹 대상 지정 + 앞뒤 자동 추적(TrackObject)**, 확대·축소·원본 크기, 배속·정지·±초 이동·프레임 번호 이동
- [ ] G4-05 대결자·AD/LDAP 옵션, GS인증·보안 점검 준비 — 부분: 결재자 PIN 본인 확인(PBKDF2, 5회 실패 10분 잠금, 감사 로그). 대결자·AD/LDAP는 미구현
- [ ] G4-06 조달 등록 자료

## G5 V2 (8주)
- [ ] G5-01 SAM 2/EdgeTAM 세그먼트 마스크·전신 연동
- [ ] G5-02 배치 큐·헤드리스 서버 모드
- [ ] G5-03 macOS(CoreML) 빌드
- [ ] G5-04 B2C 간편 모드·URL 입력·구독 라이선스
- [ ] G5-05 TensorRT EP·4K

## 열린 질문 (개발 중 추가)
- [ ] 기관 사용자 인증 방식(PIN vs AD) 1차 PoC 기관 요구 확인
- [ ] 보고서 서식: 기관 공문 서식 준수 여부
- [x] **배포 FFmpeg 라이선스** (해결: LGPL FFmpeg로 PyAV 직접 빌드, `check_license.py --packaging` 통과): PyPI `av` 휠은 libx264/libx265(GPL)를 포함한다. 코드는 GPL 인코더를 쓰지 않지만 배포본에 GPL 바이너리가 들어가므로, LGPL FFmpeg로 PyAV를 직접 빌드할지(권장) 결정 필요. `check_license.py --packaging`이 현재 실패로 막고 있음
- [x] **번호판 모델** (해결: RT-DETRv2 번호판 1차 모델 + 차량 하단 폴백 병행. 국내 파인튜닝은 G4-01): 허용 라이선스의 국내 번호판 검출 모델이 없어 차량 하단 영역 폴백을 쓴다(과마스킹). AI Hub 학습(docs/07) 전 PoC에서 이 수준을 허용할지
- [x] **결재자 본인 확인** (해결: 결재자별 PIN, 현재 단계 결재자만 승인·반려 가능): MVP는 Windows 계정 + 결재선의 이름을 기록만 한다(한 PC에서 여러 결재자가 승인 가능). PIN(V1) 전 PoC에서 허용되는지
- [x] **재검사 정의 변경** (2026-10-01 승인, docs/03 반영): 03 스펙("출력본을 conf 0.2로 재검사")을 그대로 쓰면 모자이크 자체가 얼굴로 재검출되어 끝나지 않는 루프가 생긴다. 2패스 합의 + 무결성 검사로 바꿨다(docs/11 §3) — 승인 필요
- [ ] **재검사 프레임 간격**: CPU에서 전 프레임 재검사는 분석보다 오래 걸려, 분석 검출 간격과 같은 간격으로 재검사한다(무결성 검사는 전 프레임). GPU 설치본은 간격 1로 할지
- [x] **한글 폰트 번들** (해결: 실행 PC의 Windows 기본 맑은 고딕 사용 — 재배포하지 않음. 비Windows는 `packaging/fonts/`): 보고서 PDF가 현재 Windows 맑은 고딕을 임베드한다. 배포본은 OFL 폰트(Noto Sans KR 등)를 `packaging/fonts/`에 넣을 것
- [ ] **번호판 OCR 없음**: `plate_text` 규칙은 저장되지만 OCR(V1) 전에는 일치 트랙이 없다
- [x] **ByteTrack 안전 편차** (2026-10-01 승인, docs/03 반영): 저신뢰(0.3~0.6) 검출도 새 트랙 생성
- [x] **proto 하위 호환 확장** (2026-10-01 승인, docs/05 반영): AllowRoot·Health·keep_stored·FrameRequest.profile·exposure 이벤트 등
- [x] **얼굴 검출 임계 변경** (2026-10-02 승인: 0.30 → **0.15**, docs/03 반영): 재검사(0.20)보다 낮춰 임계 부근 소형 얼굴에 여유. 군중 실영상에서 0.2~0.3 얼굴이 대량 노출로 잡혀 분석 단계에서 더 많이 가리는 쪽으로 바꿨다(오탐 마스킹 증가)
- [x] **최소 식별 얼굴 크기** (2026-10-02 결정: 폭 **16px** 미만은 재검사 노출에서 제외 — 마스킹은 그대로, docs/03 반영): Shibuya 군중 타임랩스(720p, 프레임당 얼굴 ~300개)에서 개선 후에도 재검사 얼굴 노출 317건(커버리지 약 99.8%) — 대부분 폭 ~10px·신뢰도 ~0.29. 번호판의 '20px 이하 미인식 영역'(docs/07)처럼 얼굴도 재검사에서 제외할 최소 크기(예: 12~16px)를 둘지, 아니면 이런 장면은 '전체 영역 마스킹'으로 처리할지 기관 기준 필요
- [x] **CPU 성능 목표** (2026-10-02 결정: 스펙대로 CPU 분석 ≥6 FPS를 목표로 `cpu` 프로파일 조정 — 결과 docs/11 §4): 스펙 CPU ≥6 FPS는 원해상도 얼굴 타일 + 번호판 모델 + 0.2 임계에서 달성 불가(720p 합성 2.5 FPS, 군중 타임랩스 0.57 FPS). 정확도 우선으로 두었다 — GPU 설치본을 권장할지, CPU용 경량 프로파일을 따로 둘지
- [ ] **실영상 테스트 데이터 라이선스**: `tests/data/synthetic/mixkit-*.mp4`는 Mixkit 무료 라이선스(원본 재배포 금지) → git 제외(.gitignore). CI 회귀에는 쓰지 않는다
- [x] **재검사 오탐 처리 정책** (2026-10-02 결정: (a) 검수자 '노출 아님' 확인 + 감사 로그 → 통과 인정. 권장안대로 신뢰도 ≥0.5는 확인 불가, docs/03 반영): 실영상 7편 중 2편만 검수 2회 안에 0건. 야간·우천 영상의 남은 재검사 히트는 다리·노면 반사·캔 등 검출기 오탐(신뢰도 0.2~0.55). 선택지: (a) 검수자가 히트별로 '노출 아님'을 확인하면(이름·사유 감사 로그) 통과로 인정 (b) 계속 '노출 영역 마스킹'만 허용(오탐도 가림, 수렴 느림) (c) 재검사 임계를 0.3 이상으로. 권장: (a) + 고신뢰(≥0.5) 히트는 확인 불가·반드시 마스킹

