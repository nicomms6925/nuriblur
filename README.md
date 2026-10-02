# NuriBlur

동영상에서 지정한 대상만 남기고 나머지 얼굴·번호판을 자동 마스킹하는 Windows PC 프로그램 (로컬 처리 전용).
설계 문서는 `docs/00~11`, 이번 구현의 계획·결정·결과는 [`docs/11-IMPLEMENTATION-PLAN.md`](docs/11-IMPLEMENTATION-PLAN.md).

## 개발 환경 (uv)
```bash
uv sync
```
```bash
uv run python scripts/gen_proto.py
```
- 의존성은 `pyproject.toml` + `uv.lock`으로 고정된다. 패키지 추가는 `uv add <패키지>`, 개발용은 `uv add --dev <패키지>`.
- 모델(`models/*.onnx`, Git LFS)은 `models/manifest.json`의 해시·라이선스와 일치해야 로드된다.
- GPU 추론: `onnxruntime`을 같은 모듈 이름의 GPU 빌드로 교체한다(동시 설치 불가).
  NVIDIA `uv pip uninstall onnxruntime; uv pip install onnxruntime-gpu`, AMD/Intel은 `onnxruntime-directml`.

## 실행
```bash
uv run python -m app.main
```
- 기본은 기관 모드(6단계 결재·리포트). 유튜버용은 `NURIBLUR_EDITION=b2c`.
- UI가 워커 프로세스(gRPC, 127.0.0.1 + 세션 토큰)를 자동으로 띄운다.
- 기관 DB(결재선·결재자·감사 로그)는 `%PROGRAMDATA%\NuriBlur\org.sqlite` (`NURIBLUR_ORG_DB`로 변경). 처음 실행한 Windows 계정이 관리자.
- 결재: 6단계 "결재자 관리"에서 결재자와 초기 PIN을 등록한 뒤 결재선을 저장한다. 승인·반려·검수 완료는 해당 단계 결재자의 PIN으로만 가능하다.

### 영상 보기·검수 조작
| 동작 | 버튼 / 키 |
|---|---|
| 재생·일시정지 / 정지(재생 시작 위치로) | ▶ · Space / ■ |
| 이전·다음 프레임 / ±1초 / ±10초 | ‹ › · ← → / Shift+← → / −10 +10 · Ctrl+← → |
| 처음·끝 | ⏮ ⏭ · Home End |
| 배속 | 0.25× ~ 4× (느리면 프레임을 건너뛰어 실제 시간에 맞춤) |
| 프레임 번호로 이동 | "프레임" 입력칸, 슬라이더, 타임라인 클릭·드래그, Shift+휠 |
| 확대·축소 / 맞춤 / 원본 크기 | 휠 · + − / 맞춤 · 0 / 원본 · 1 (확대 시 드래그로 이동) |
| 재검사 노출 한 번에 처리 | 검수 목록 맨 위 "모두 마스킹하고 다시 내보내기" (자동 최대 3회 반복) |
| 병합 제안 한 번에 | 검수 목록 "n건 모두 병합" (보호대상과 무관한 제안만, 보호 관련은 하나씩) |
| 재설정 | 3단계 "보호·마스킹 설정 초기화" / "처음부터 다시 분석" |
| 재검사 노출 확인 | 검수 목록의 "보기"(출력 화면에 빨간 박스) → "마스킹" 또는 "노출 아님"(사유 입력·감사 로그, 신뢰도 0.5 미만만). 남은 노출 0건이면 통과 |
| 객체 목록(영상 전체) | 3·4단계 "객체 목록" → 같은 객체끼리 묶인 썸네일에서 여러 개 선택 → 마스킹 제외 / 객체 아님(오검출) / 마스킹 |
| 지정한 사람·차량만 남기고 전부 가리기 | 3단계 "보호대상 외 전체 가리기" 체크 → 남길 사람(B#)·차량(V#) 박스 클릭 (얼굴·번호판 함께 보호) |
| 놓친 얼굴·번호판 가리기 | "＋ 마스킹 대상 지정" → 클릭 또는 드래그 → 얼굴/번호판/기타 선택 → 앞뒤 자동 추적해 전 구간 마스킹 (Esc 취소) |

## CLI (헤드리스)
```bash
uv run nuriblur analyze in.mp4 -o case.nbproj
```
```bash
uv run nuriblur rules case.nbproj --protect 3,7
```
```bash
uv run nuriblur render case.nbproj -o out.mp4 --audit
```
종료 코드: 0 성공 · 3 재검사 노출 · 4 모델 라이선스/해시 오류. 노출이 나오면 `rules case.nbproj --mask-exposures` 후 다시 render,
오탐이면 확인 기록 후 통과(신뢰도 0.5 미만 얼굴·번호판만):
```bash
uv run nuriblur dismiss case.nbproj -o out.mp4 --all --reason "노면 반사"
```
- 재검사는 폭 16px 미만 얼굴(식별 불가)을 노출로 세지 않는다(마스킹은 그대로).

## 테스트·회귀
```bash
uv run pytest -q -m "not slow and not ui"
```
```bash
uv run pytest -q
```
```bash
uv run python scripts/regress.py tests/data
```
- `tests/test_exposure_audit.py`(노출 재검사 게이트)가 깨지면 머지 불가.
- 실제 영상 회귀: `tests/data/{장르}/`에 영상을 넣고 `scripts/regress.py` → 영상별 분석/렌더 속도·재검사 노출·대조 시트.
  GT 초안은 `scripts/make_gt.py <영상>` (사람이 확인 후 `verified: true`).

## 배포본 빌드
```bash
powershell -ExecutionPolicy Bypass -File scripts/build/build_release.ps1
```
uv 환경 → LGPL FFmpeg로 빌드한 PyAV로 교체(`scripts/build/build_lgpl_pyav.ps1`) → `check_license.py --packaging` 게이트 →
PyInstaller → Inno Setup(온라인·폐쇄망 `/DOFFLINE`). 결과는 `.build/installer/`.

## 현재 한계 (자세히는 docs/10 열린 질문)
- 번호판: 1차 모델은 Open Images로 학습된 범용 모델(국내 번호판 미학습). 못 찾은 차량은 하단 영역을 보수적으로 마스킹.
- 얼굴 신원 매칭(참조 사진)·번호판 OCR은 V1.
- CPU에서는 군중·고해상도 영상이 느리다(얼굴 원해상도 타일 + 번호판 모델). GPU 측정은 GPU PC에서 `scripts/bench.py --profile gpu_precise`.
