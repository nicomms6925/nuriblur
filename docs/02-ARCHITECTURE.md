# 02. 아키텍처

## 한 줄 요약
UI(PySide6) · 처리 코어(Python gRPC 워커) · 모델/저장 계층의 3계층. UI는 이벤트만 구독하고, 분석 결과(트랙)는 SQLite에 캐시되어 렌더링을 반복한다.

```
┌─────────────────────── 데스크톱 UI (PySide6) ───────────────────────┐
│ 입력·큐 │ 진행 모니터 │ 보호대상 선택 │ 검수 타임라인 │ 내보내기 │ 결재·리포트 │
└────────────────────────────┬────────────────────────────────────────┘
          작업 큐 · 진행 이벤트 (gRPC, 127.0.0.1, 세션 토큰) — 별도 프로세스
┌────────────────────────────▼────────────────────────────────────────┐
│ 처리 코어 (worker/)  분석 1회 → 렌더링 N회                              │
│ 디코딩 → 검출 → 추적 → (임베딩·OCR) → 매칭 → 마스킹 → 인코딩 → 재검사       │
│ orgmode/: 결재선·감사로그·보고서                                       │
└────────────────────────────┬────────────────────────────────────────┘
          ONNX Runtime (CUDA / DirectML / CoreML EP) — 로컬 전용
┌────────────────────────────▼────────────────────────────────────────┐
│ 모델·저장: YOLOX(얼굴·전신·번호판) │ ByteTrack │ ArcFace·OCR │ SAM2(V2)  │
│           트랙 캐시 SQLite(.nbproj) │ 기관 DB(approval/audit)         │
└──────────────────────────────────────────────────────────────────────┘
```

## 프로세스 모델
| 프로세스 | 역할 | 생명주기 |
|---|---|---|
| `nuriblur-app` (PySide6) | 화면, 상태 머신, gRPC 클라이언트, 프로젝트 파일 관리 | 사용자 세션 |
| `nuriblur-worker` (gRPC) | 모든 추론·인코딩. 작업 1개 = 스레드 풀(디코더 1, 추론 1, 인코더 1, 추적 1) | UI가 시작/종료, 크래시 시 UI가 재기동하고 작업 재개 |
| `nuriblur` CLI | 워커를 in-process로 호출하는 헤드리스 진입점 (테스트·배치) | 1회 실행 |

- UI ↔ 워커 통신: Unix/TCP 127.0.0.1, 포트는 UI가 랜덤 할당 후 환경변수로 전달, 세션 토큰 메타데이터 필수.
- 프레임 미리보기는 480p JPEG를 이벤트로 전달(2 FPS). 전체 프레임은 전달하지 않는다.
- 워커가 죽으면 UI는 `.nbproj`의 `job.checkpoint_frame`부터 재개 요청.

## 모듈 경계 (worker/)
```
worker/
├── server.py          # gRPC 서비스 구현, 세션 토큰 검사
├── pipeline/
│   ├── probe.py       # ffprobe 래퍼, VFR→CFR 판단, 회전 메타
│   ├── decode.py      # PyAV 디코더 스레드, 링버퍼(64)
│   ├── detect.py      # ONNX 검출기(배치), 클래스: face/person/plate
│   ├── track.py       # ByteTrack 포팅 + 칼만 보간 + 패딩
│   ├── identify.py    # ArcFace 임베딩(키프레임 3장), OCR 다수결
│   ├── rules.py       # protect_rule → track_decision (deny-by-default)
│   ├── mask.py        # 픽셀화/블러/단색/세그먼트, EMA 스무딩
│   ├── encode.py      # FFmpeg 인코더(NVENC/QSV/VT/openh264), 오디오 copy, 메타 제거
│   └── audit_check.py # 노출 재검사
├── models/registry.py # manifest.json 로드·해시·라이선스 검증, EP 선택
├── io/project.py      # .nbproj 읽기/쓰기(SQLite+썸네일 zip)
└── orgmode/           # approval.py, auditlog.py, report.py(PDF)
```

## 런타임 선택
| 환경 | Execution Provider | 모델 프로파일 |
|---|---|---|
| Windows + NVIDIA | CUDA EP (TensorRT EP는 V2 옵션) | YOLOX-s fp16, 입력 1280 |
| Windows + AMD/Intel | DirectML EP | YOLOX-tiny, 입력 960 |
| CPU 전용 | CPU EP, 검출 간격 4프레임 | YOLOX-nano, 입력 640 |
| macOS (V2) | CoreML EP | YOLOX-s |

## 상태 머신 (작업 1건)
```
CREATED → PROBED → ANALYZING ⇄ PAUSED → ANALYZED → RULES_APPLIED → REVIEWING
        → RENDERING ⇄ PAUSED → AUDITED(재검사 통과) → PENDING_APPROVAL → APPROVED → DELIVERED → RETAINED → PURGED
        └ 어느 단계든 CANCELLED / FAILED(원인 코드)
```
- `REJECTED`(반려)는 `REVIEWING`으로 되돌린다. 재렌더링 시 `RENDERING`부터.
- `AUDITED`는 `audit_check`가 0건일 때만 진입. 1건 이상이면 `REVIEWING`으로 돌아가며 노출 프레임 목록을 검수 큐에 넣는다.
