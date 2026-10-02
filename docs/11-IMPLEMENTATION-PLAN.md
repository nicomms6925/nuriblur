# 11. 구현 계획 (G0~G3 MVP)

## 한 줄 요약
인계 문서(00~10)를 그대로 따르되, 아직 없는 자체 학습 모델은 라이선스를 지키는 공개 모델로 대체하고, 모든 모델을 `manifest.json` 레지스트리로 교체 가능하게 만든다.

## 0. 현황 분석

| 항목 | 상태 | 영향 |
|---|---|---|
| 코드 | 없음 (문서·proto·목업만) | G0부터 전부 구현 |
| GPU | 없음 (개발 PC는 원격 디스플레이 어댑터) | CPU EP로 개발·검증. CUDA/DML은 EP 선택 로직만 구현, 성능 목표(60 FPS)는 RTX 4060에서 별도 측정 |
| FFmpeg 바이너리 | 없음 | ffprobe 대신 **PyAV**(번들 FFmpeg)로 probe·decode·encode 일원화 |
| Python | 3.14 기본, 3.11은 uv 관리 | `uv venv --python 3.11 .venv` |
| 자체 학습 YOLOX(face/person/plate) | 없음 (AI Hub 학습 필요, G4) | 아래 대체 모델 |
| 국내 번호판 모델·OCR | 없음 (G4) | 차량 기반 보수적 번호판 폴백 |
| ArcFace 자체 학습 | 없음 (G4) | 병합 제안은 외형 특징(색·밝기 히스토그램)으로 대체, MatchReference는 UNIMPLEMENTED |

### 대체 모델 (모두 manifest 등록·해시 검증)
| id | 파일 | 라이선스 | 용도 |
|---|---|---|---|
| `face_yunet_2023mar` | OpenCV Zoo YuNet | MIT | 얼굴 검출 + 노출 재검사 |
| `yolox_nano_coco` | Megvii YOLOX-nano (416) | Apache-2.0 | 전신(person)·차량(car/bus/truck/motorcycle), CPU/GPU 빠름 프로파일 |
| `yolox_s_coco` | Megvii YOLOX-s (640) | Apache-2.0 | GPU 정밀 프로파일 |
| `plate_from_vehicle` | 가중치 없음(규칙) | 자체 | 차량 박스 하단 영역을 번호판 후보로 마스킹 (`audit_capable=false`) |

> 번호판 전용 모델이 생기면 manifest에 `task: "plate"`, `audit_capable: true`로 추가하면 검출·재검사 모두 자동으로 교체된다.

## 1. 저장소 구조 (CLAUDE.md 디렉터리 준수)
```
nuriblur-handoff/            ← 저장소 루트
├── pyproject.toml  Makefile  .pre-commit-config.yaml  .github/workflows/ci.yml
├── proto/nuriblur.proto
├── worker/
│   ├── pb/                  # 생성 코드 (make proto)
│   ├── server.py            # gRPC 서비스·토큰 인터셉터·경로 검사·작업 스레드
│   ├── cli.py               # nuriblur probe/analyze/rules/render/audit/worker
│   ├── jobs.py              # JobControl(pause/resume/cancel), 이벤트 버스
│   ├── errors.py            # E_* 오류 코드 예외
│   ├── pipeline/ probe decode detect track identify link rules mask encode audit_check analyze render frames
│   ├── models/registry.py   # manifest 로드·해시·라이선스·EP 선택·프로파일
│   ├── io/project.py        # .nbproj (zip: project.sqlite + thumbs/ + manifest.json)
│   └── orgmode/ approval.py auditlog.py report.py retention.py db.py
├── app/
│   ├── main.py  theme.py  i18n/{__init__.py,ko.json}
│   ├── state/machine.py     # 02-ARCHITECTURE 작업 상태 머신
│   ├── worker_client.py     # 워커 프로세스 기동/재기동, gRPC 스트림 → Qt 시그널
│   ├── session.py           # 큐 항목(영상 1개) UI 측 상태
│   ├── views/ main_window.py(컨트롤러) topbar.py queue_panel.py stage_view.py timeline.py side_panels.py(1~6단계)
│   └── widgets/ common.py
├── models/manifest.json + *.onnx
├── scripts/ gen_proto.py bench.py make_test_clips.py ci/check_license.py ci/check_manifest.py
├── tests/  data/README.md + assets/  test_*.py
└── packaging/ pyinstaller.spec installer.iss
```

## 2. 게이트별 작업 계획

### G0 뼈대
| ID | 구현 내용 | 완료 기준 |
|---|---|---|
| G0-01 | pyproject(의존성·entry point `nuriblur`, `nuriblur-app`), ruff·mypy 설정, pre-commit | `ruff check` 통과 |
| G0-02 | `scripts/gen_proto.py`(grpcio-tools) + `make proto` → `worker/pb`, `app/pb` (상대 import 패치) | 생성 코드 import 성공 |
| G0-03 | manifest 스키마(id·file·sha256·license·source·task·classes·input_size·audit_capable) + `registry.py` | 해시 불일치→E_MODEL_HASH, 금지 라이선스→E_MODEL_LICENSE 테스트 |
| G0-04 | CI 워크플로(ruff·pytest·check_license: pip-licenses AGPL/SSPL 차단, `import ultralytics` 차단, manifest 검사) | 로컬 실행 통과 |
| G0-05 | `tests/test_exposure_audit.py`, `scripts/bench.py` | G1에서 통과 상태로 전환 |
| G0-06 | `tests/data/{genre}/{clip}.mp4 + .gt.json` 규약 문서, 합성 클립 생성기 | README·생성 스크립트 |

### G1 워커 CLI
| ID | 구현 내용 |
|---|---|
| G1-01 probe | PyAV로 코덱·해상도·FPS·프레임 수·길이·오디오, 회전(display matrix / `rotate` 태그), VFR 판정(PTS 간격 표준편차), SHA-256 |
| G1-02 decode | 디코더 스레드 + 링버퍼(64), 회전 적용, HW 디코드 시도 → 실패 시 SW + `W_HWDEC_FALLBACK`. 프레임 PTS 테이블(`frame_pts`)을 저장해 GetFrame 랜덤 접근에 사용 |
| G1-03 detect | ORT 세션(EP 자동 선택), YOLOX 후처리(grid·stride·NMS), YuNet 후처리(stride 8/16/32), 클래스별 conf(face 0.30, person 0.35, plate 0.25), 검출 간격(기본 2, CPU 4) |
| G1-04 track | ByteTrack 자체 포팅(칼만 xyah, high 0.5/low 0.1, match 0.8, buffer 30), 클래스별 독립, 사이 프레임은 칼만 예측(interpolated=1), 트랙 내부 공백은 선형 보간 |
| G1-05 link·merge | 얼굴↔전신 (상단 40% IoA≥0.7, 시간 겹침≥60%). 병합 제안: 공백≤2초, 외형 코사인≥0.5, 중심 거리≤폭×2 |
| G1-06 .nbproj | zip(project.sqlite+thumbs+manifest), 원자적 저장(tmp→replace), checkpoint_frame 주기 저장, 재개 시 체크포인트 이후만 분석(트랙 ID 오프셋, 병합 제안으로 연결) |
| G1-07 rules | deny-by-default. click/merged 승계/linked person 승계, plate_text(편집거리), region·timerange 제외, manual_box(추가 마스킹). 신뢰도<임계 → 보호하지 않고 `REVIEW` |
| G1-08 mask | pixelate(block=w/8≥8px)·gaussian(σ=w/4≥15)·solid, ×1.25 확장, EMA α=0.6, 앞뒤 패딩, 번호판<20px → ×3 폴백, 얼굴 없는 전신은 머리 영역 보수 마스킹(옵션, 기본 on) |
| G1-09 encode | 인코더 우선순위 NVENC→QSV→AMF→MediaFoundation(h264_mf)→libopenh264→mpeg4. **libx264 등 GPL 인코더는 사용 금지**. 비트레이트=원본×1.0(상한 ×1.5), 오디오 패킷 copy, 메타데이터 미복사, PTS 보존 |
| G1-10 audit | 출력본을 face 검출기(conf 0.2)로 전 프레임 재검사(+audit_capable 번호판 모델). 보호 트랙과 IoU≥0.3이면 제외, 적용된 마스크 영역에 80% 이상 덮인 검출은 "가려진 것"으로 제외. 나머지는 노출 → 종료코드 3 |
| G1-11 gRPC | 127.0.0.1 바인딩, `x-nb-token` 인터셉터, 허용 루트 경로 검사, Analyze/Render 스트림, Control, GetFrame(원본/마스킹), ListTracks(at_frame), ApplyRules, MergeTracks, AuditCheck |
| G1-12 CLI | `probe/analyze/rules/render/audit/worker`, 종료코드 0/3/4 |
| G1-13 포맷 매트릭스 | 합성 클립: mp4/mkv/mov × h264/mpeg4 × 회전 90 × VFR → probe·analyze·render 왕복 |
| G1-14 bench | `scripts/bench.py` 분석/렌더 FPS·누락률(GT 대비) JSON 출력 |

### G2 UI (PySide6, 목업 v0.3 그대로)
| ID | 구현 내용 |
|---|---|
| G2-01 | QMainWindow: TopBar(로고·6단계 스테퍼·EP 배지·로컬 처리 배지) / QSplitter(Queue 260 · Stage · Side 320) / 하단 Timeline Dock(160). 색 토큰 QSS, 다크 기본·라이트 자동, 문구는 `app/i18n/ko.json`만 |
| G2-02 | 상태 머신(전이표 검증), WorkerClient: 랜덤 포트·32바이트 토큰으로 워커 서브프로세스 기동, 스트림은 QThread → 시그널, 워커 크래시 시 재기동 후 checkpoint 재개 |
| G2-03 | 1단계: 드롭존(다중)·파일 추가·큐 목록(상태 배지)·메타·검출 대상·프로파일·분석 시작 |
| G2-04 | 2단계: 진행률·ETA·5단계 체크·게이지(FPS/GPU/VRAM)·카운터·로그(레벨 색)·2 FPS 미리보기·일시정지/재개/취소 |
| G2-05 | 3단계: QGraphicsView 프레임 + 박스 오버레이(색+태그 `F#3 ✓`/`F#12 ?`/`P#4`) 클릭 토글, "이 프레임의 트랙" 목록, 카운터, 번호판 텍스트 입력 |
| G2-06 | 타임라인: 트랙 레인(색=판정, 점선=끊김), 룰러, 플레이헤드, 클릭·드래그 스크러빙 → GetFrame |
| G2-07 | 4단계: 검수 목록(끊김 병합·REVIEW·누락 의심=얼굴 없는 전신), 수동 박스(2 키프레임 보간), 원본/마스킹/좌우 비교 |
| G2-08 | 5단계: 스타일·강도·여백(하한 표시)·코덱·품질·오디오·메타 제거·감사 로그 JSON·저장 위치 → Render 스트림 → 재검사 통과 시 6단계 자동 이동 |
| G2-09 | pytest-qt: 창 생성·단계 전환·상태 머신, 실제 워커로 분석→보호→렌더 시나리오, 취소·워커 재기동 |

### G3 기관 모드
| ID | 구현 내용 |
|---|---|
| G3-01 | `org.sqlite`(04 스키마) + 결재선 프리셋 2/3/4단(최대 6단)·관리자만 저장·처리 건 생성 시 스냅샷 |
| G3-02 | 결재 흐름: 현재 단계 승인(역할·이름 표시)·반려(사유 필수, 전 단계 초기화, 상태 REVIEWING)·최종 승인 후에만 출력 제공 |
| G3-03 | 감사 로그: sha256 해시 체인, `BEFORE UPDATE/DELETE` 트리거 ABORT, 체인 검증, JSON/CSV 내보내기 |
| G3-04 | 보고서 PDF(reportlab, 한글 TTF 임베드, 서명란=결재 단계 수+출력 제공), 열람 전용 워터마크(렌더 시 합성), 출력본 해시, 보관 만료 삭제(출력본·프로젝트 삭제, 로그·PDF 보존) |
| G3-05 | `packaging/pyinstaller.spec` + `installer.iss`(온라인/폐쇄망 2종, 폐쇄망은 네트워크 기능 off 플래그) — 빌드 실행은 범위 밖 |
| G3-06 | PoC는 범위 밖 (현장 필요) |

## 3. 핵심 설계 결정
1. **프레임 인덱스 = 디코드 순서**. VFR도 PTS를 그대로 보존해 출력하므로 입력·출력 프레임 수가 같고, 재검사는 같은 인덱스로 트랙과 대조한다.
2. **.nbproj 작업 사본**: zip을 임시 작업 폴더에 풀어 SQLite로 작업하고, 변경 시 원자적으로 다시 묶는다. 작업 폴더는 워커 종료 시 삭제(임시 파일 즉시 삭제 원칙).
3. **노출 재검사 판정 (구현하며 바꿈 — 2026-10-01 승인)**
   - 03 스펙대로 출력본을 conf 0.2로 그대로 재검사하면, 검출기가 **모자이크 자체를 얼굴로 다시 잡고**(박스가 마스크보다 크게 나옴) 마스크를 더해도 다시 잡히는 루프가 생긴다. 커버리지 비율 기준도 실험에서 오탐이 많았다.
   - 구현: **2패스 합의** — (A) 출력 그대로, (B) 마스크 영역을 회색으로 덮은 출력. B의 검출 중 A에도 같은 자리에 있는 것만 노출. 실제로 보이는 얼굴은 양쪽에서 잡히고, 모자이크 재검출(A만)·회색 경계 오검출(B만)은 걸러진다. 평균색으로 덮으면 피부색 덩어리가 얼굴로 잡혀 회색을 쓴다.
   - 보호 판정: 보호 박스와 IoU ≥0.3 **또는** 검출의 70% 이상이 보호 박스(×1.25) 안 — 보호 얼굴 일부가 이웃 마스크에 가려 작게 따로 잡히는 경우.
   - **무결성 검사**: 원본을 함께 디코딩해 같은 계획으로 '기대 출력'을 만들고, 마스크 영역이 기대 출력보다 원본에 가까우면 `mask_missing`. (B에서 마스크를 덮어 버리므로 인코딩·정렬 오류는 여기서 잡는다.) 출력 프레임 수 불일치도 노출로 센다.
   - 민감도 검증(`tests/test_exposure_audit.py`): 원본을 출력으로 / 분석 누락(트랙 삭제) / 마스크 55% 어긋남 / 마스크 미적용 — 모두 노출로 잡힌다. 깨끗한 렌더는 0건.
   - 재검사에서 노출이 나오면 검수의 "노출 영역 마스킹"(앞뒤 몇 프레임 수동 박스) → 재렌더링으로 해소한다.
6. **ByteTrack 안전 편차 (2026-10-01 승인)**: 원본은 conf ≥0.6 검출만 새 트랙을 만들고 미확정 트랙을 바로 버려, 스펙의 얼굴 임계 0.30과 충돌(0.3~0.6 얼굴이 마스킹되지 않음). 연관되지 않은 모든 검출로 트랙을 만들고, 미확정 트랙은 검출 3회분 동안 유지·재연결한다.
7. **EMA 지연 방지**: 좌표 EMA(α=0.6)로 부드럽게 한 박스와 원 박스의 합집합을 마스킹한다(빠르게 움직이는 얼굴이 EMA 지연으로 삐져나오지 않도록).
4. **기관 모드는 추론이 없으므로** UI가 `worker.orgmode`를 직접 import해 `org.sqlite`를 다룬다(proto 확장 없음). `worker/__init__.py`는 ORT를 import하지 않는다.
5. **안전 기본값**: 번호판 모델 부재 시 차량 하단 영역 마스킹, 얼굴 미검출 전신은 머리 영역 마스킹, REVIEW는 항상 마스킹 유지.

## 4. 결과 (이 PC: CPU 8코어, GPU 없음) — 2026-10-01 2차
| 항목 | 값 |
|---|---|
| 테스트 | 87개 통과 (단위 + 노출 게이트·번호판 민감도·포맷 매트릭스·gRPC·CLI·결재 PIN·UI 시나리오·보기 조작) |
| 합성 회귀 클립(720p 6초, 얼굴 4·번호판 1) | **얼굴 누락률 0% · 번호판 누락률 0% · 재검사 노출 0건** |
| 실영상: Shibuya 군중 타임랩스 720p (Mixkit, 594프레임, 프레임당 얼굴 ~300) | 재검사 얼굴 노출 6,407 → **317** (적응형 검출 간격·원해상도 타일·보간 프레임 재검사·히스테리시스). 남은 것은 폭 ~10px·신뢰도 ~0.29 — 최소 식별 크기 정책 필요(docs/10) |
| 배포 | LGPL FFmpeg PyAV 휠 빌드, 라이선스 게이트 통과, PyInstaller exe(워커 모드 검증), Inno Setup 온라인/폐쇄망 설치 파일(각 247MB) |
| 개발 환경 | uv 프로젝트(`uv.lock`, `uv sync`, `uv run`), CI도 `uv sync --locked` |

### 실영상 7편 회귀 (Mixkit, CPU) — 재검사 → 검수 '노출 영역 마스킹' 반복
| 영상 | 분석 FPS | 1차 재검사 | 검수 1회 | 검수 2회 | 남은 것 |
|---|---|---|---|---|---|
| 인물 클로즈업(1080p) | 4.9 | 9 | **0** | | — |
| 회의실(1080p) | 4.8 | 89 | 3 | **0** | — |
| 지하철 입구(1080p) | 2.1 | 228 | 16 | 2 | 얼굴 2 |
| 도쿄 야간 거리(1080p) | 4.0 | 166 | 39 | 14 | 얼굴 12·번호판 2 |
| 타임스스퀘어 우천 야간(720p) | 3.3 | 178 | 91 | 56 | **모두 오탐**(다리·젖은 노면 반사·캔·택시) |
| 군중 타임랩스(720p) | 0.35 | 226 | 54 | 33 | 얼굴 33 (폭 ~10px 소형). 1080p는 분석 0.04 FPS로 CPU 처리 불가 |

- 1차 재검사의 대부분은 **이미 모자이크된 옆모습 머리 윤곽 재검출**과 손·배경 보케 오탐 → 검출 중심부 70% 이상이 마스크 아래면 제외(core_covered)로 회의실 142 → 89.
- 야간·우천 영상은 검출기(신뢰도 0.2) 잡음이 지속적으로 남아 '0건'까지 자동 수렴하지 않는다 → 검수자 '오탐 확인' 정책 필요(docs/10 열린 질문).

### 2026-10-02 3차: 결정 반영 후 재측정 (cpu 프로파일: 검출 간격 4 · 번호판 모델 3검출마다, 폭 16px 미만 얼굴 재검사 제외)
| 영상 | 프레임 | 분석 FPS (전체 실행) | 렌더 FPS | 1차 재검사 (이전 → 지금) |
|---|---|---|---|---|
| 인물 클로즈업(1080p) | 360 | 3.8 | 8.5 | 9 → 10 |
| 회의실(1080p) | 848 | 4.6 | 15.3 | 89 → 81 |
| 지하철 입구(1080p) | 150 | 3.4 | 14.5 | 228 → 137 |
| 도쿄 야간 거리(1080p) | 559 | 5.7 | 24.5 | 166 → 99 (번호판 6) |
| 타임스스퀘어 우천 야간(720p) | 596 | 5.4 | 40.7 | 178 → 154 (번호판 2) |
| 군중 타임랩스(720p) | 594 | 0.96 | 25.1 | 226 → 143 |
| 군중 타임랩스(1080p) | 594 | 0.64 | 4.4 | 처리 불가(0.04 FPS) → **51** |

- 분석 FPS는 모델 로딩 이후 ~ 저장까지 전체 시간 기준(링크·규칙·저장 후처리 포함). 300프레임 구간 측정에서는
  도쿄 7.6 · 타임스스퀘어 6.9 · 지하철 5.6 FPS였다. 개발 PC는 8 vCPU 가상머신(EPYC 7282)이고 다른 프로세스가
  상시 CPU 일부를 쓴다 → **전체 실행 기준 6 FPS는 아직 미달(3.4~5.7)**. 남은 비용: 얼굴 원해상도 타일(1080p 9회),
  RT-DETR 번호판 모델(CPU 1회 ≈0.3초). 추가 단축은 번호판 모델 경량화(새 모델 = 다운로드 승인 필요) 또는 GPU.
- 군중 타임랩스는 적응형 간격(매 프레임 검출) 때문에 CPU로는 1 FPS 미만 — GPU 권장.
- 후처리 병목 제거: 병합 제안(전수 비교 → 정렬·이분 탐색), 썸네일(개별 파일 → zip 직접 기록), 얼굴↔전신 연결(프레임 색인).
  군중 1080p 40프레임 기준 242초 → 55초.
- 남은 재검사 항목은 검수 화면의 '마스킹' 또는 '노출 아님'(신뢰도 0.5 미만, 사유·감사 로그)으로 처리한다.
- 알려진 문제: 군중 1080p 프로젝트 파일 316MB(얼굴 트랙 약 9.8만 개의 썸네일) — 짧은 잡음 트랙 썸네일 생략 검토.

### 실영상에서 발견해 고친 안전 문제
1. **타임랩스·빠른 이동**: 얼굴이 프레임당 ~50px 이동 → 간격 검출 + 보간으로는 따라갈 수 없음 → 적응형 검출 간격(새 트랙 비율 > 50%면 매 프레임 검출).
2. **재검사가 검출 프레임만 보던 문제**: 분석과 같은 프레임만 검사하면 보간 구간 누출을 못 봄 → 검출 프레임 사이를 검사.
3. **잃은 트랙이 다른 사람에게 붙는 문제(보호 노출)**: 사라진 A의 얼굴 트랙이 같은 자리를 지나는 B에게 붙어, B를 클릭 보호하면 A 구간까지 보호됨 → 얼굴·전신은 3검출 단계만 대기, 버퍼 IoU는 연속 추적에만.
4. **1차 번호판 모델이 얼굴을 번호판으로 잡아 보호 얼굴을 가림** → 얼굴과 겹치거나 얼굴을 품은 번호판·정사각형 박스 제거.
5. **빠르게 지나가는 번호판의 트랙 앞뒤 누출** → 패딩을 트랙 속도로 외삽(제자리 박스와 합집합).
6. **압축이 심한 작은 마스크의 무결성 오탐** → 기대 변화량 대비 비율 판정, 16px 미만 영역 제외.

## 5. 범위 밖 / 후속
- G4(번호판 파인튜닝·OCR·ArcFace 자체 학습·참조 사진), G5(SAM2·배치·macOS·B2C) — 학습 데이터 필요.
- RTX 4060 성능 목표 측정, 실제 설치 파일 빌드, 기관 PoC.
