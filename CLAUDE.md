# NuriBlur — Claude Code 프로젝트 지침

> 동영상에서 지정한 대상(화이트리스트)만 남기고 나머지 얼굴·번호판을 자동 마스킹하는 Windows PC 프로그램.
> 1차 타깃: 기관 CCTV(B2B, 열람 제공·결재·감사 로그) / 2차: 유튜버(B2C).

## 먼저 읽을 것 (순서대로)
1. `docs/00-HANDOFF-README.md` — 개발 순서와 완료 기준
2. `docs/01-PRD.md` — 요구사항
3. `docs/02-ARCHITECTURE.md`, `docs/03-PIPELINE-SPEC.md`
4. `proto/nuriblur.proto`, `docs/04-DATA-MODEL.md`
5. `docs/06-UI-SPEC.md` + `mockup/nuriblur-mockup.html` (브라우저로 열어 6단계 흐름 확인)
6. `docs/09-LICENSE-COMPLIANCE.md` — 위반 시 PR 거부

## 절대 규칙
- **노출 금지 원칙**: 식별 신뢰도가 낮으면 마스킹 유지. 기본값은 deny-by-default(모두 가리고 허락된 것만 연다).
- **로컬 처리만**: 모델·영상·임베딩은 외부로 전송하지 않는다. 네트워크 호출은 URL 다운로드(yt-dlp)·모델 다운로드·업데이트 확인 3종만, 각각 사용자 동의 후. 기관 설치본은 셋 다 비활성.
- **분석(1회)과 렌더링(N회) 분리**: 트랙 DB(.nbproj)를 캐시하고, 보호대상·마스킹 설정 변경 시 재분석하지 않는다.
- **UI 프로세스에 추론 코드 금지**: 모든 추론은 `worker/` gRPC 프로세스에서. UI는 이벤트만 구독.
- **라이선스**: Ultralytics(AGPL)·InsightFace 사전학습 가중치(비상업) 사용 금지. `models/manifest.json`에 없는 모델 로드 금지. CI가 차단한다.
- **노출 재검사 게이트**: 렌더링 결과를 검출기로 재검사해 보호대상 외 얼굴·번호판이 0건이어야 "완료". `tests/test_exposure_audit.py`가 깨지면 머지 불가.
- 감사 로그는 append-only. 결재선은 처리 건 생성 시 스냅샷 저장.

## 스택
- Python 3.11, PySide6 (UI), gRPC (UI↔워커), ONNX Runtime (CUDA/DirectML/CoreML EP), OpenCV, PyAV/FFmpeg(LGPL 빌드), SQLite, PaddleOCR, ByteTrack(자체 포팅, MIT), YOLOX(Apache 2.0), SAM 2/EdgeTAM(V2).
- 패키징: PyInstaller + Inno Setup(Windows). macOS는 V2.
- 테스트: pytest, pytest-qt, 회귀 세트 `tests/data/`(git-lfs).

## 디렉터리
```
nuriblur/
├── app/        # PySide6 UI: views/ widgets/ state/ (상태 머신 기반)
├── worker/     # gRPC 워커: pipeline/ models/ io/ orgmode/
├── proto/      # nuriblur.proto → 생성 코드는 worker/pb, app/pb
├── models/     # ONNX + manifest.json (해시·라이선스·출처)
├── tests/      # 회귀·노출 감사·포맷 매트릭스·UI
├── scripts/    # 데이터 변환·파인튜닝·벤치마크
└── packaging/  # pyinstaller.spec, installer.iss
```

## 작업 방식
- 작은 PR 단위. 각 PR 제목에 `docs/10-ROADMAP-TASKS.md`의 항목 ID를 붙인다 (예: `[G1-04] ByteTrack 포팅`).
- 성능 수치(분석 FPS·렌더 FPS·누락률)는 `scripts/bench.py`로 측정해 PR 본문에 적는다.
- 한국어 주석·커밋 메시지 OK. 사용자 노출 문구는 `app/i18n/ko.json`에만.
- 모르는 것은 추측하지 말고 `docs/10-ROADMAP-TASKS.md` 하단 "열린 질문"에 적고 멈춘다.
