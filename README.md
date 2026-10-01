# NuriBlur

동영상에서 지정한 대상만 남기고 나머지 얼굴·번호판을 자동 마스킹하는 Windows PC 프로그램 (로컬 처리 전용).
설계 문서는 `docs/00~11`, 이번 구현의 계획·결정·결과는 [`docs/11-IMPLEMENTATION-PLAN.md`](docs/11-IMPLEMENTATION-PLAN.md).

## 설치 (개발)
```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe scripts/gen_proto.py
```
모델(`models/*.onnx`)은 `models/manifest.json`의 해시와 일치해야 로드된다.

## 실행
```bash
.venv/Scripts/python.exe -m app.main
```
- 기본은 기관 모드(6단계 결재·리포트). 유튜버용은 `NURIBLUR_EDITION=b2c`.
- UI가 워커 프로세스(gRPC, 127.0.0.1 + 세션 토큰)를 자동으로 띄운다.
- 기관 DB(결재선·감사 로그)는 `%PROGRAMDATA%\NuriBlur\org.sqlite` (`NURIBLUR_ORG_DB`로 변경). 처음 실행한 Windows 계정이 관리자.

## CLI (헤드리스)
```bash
.venv/Scripts/nuriblur.exe analyze in.mp4 -o case.nbproj
```
```bash
.venv/Scripts/nuriblur.exe tracks case.nbproj
```
```bash
.venv/Scripts/nuriblur.exe rules case.nbproj --protect 3,7
```
```bash
.venv/Scripts/nuriblur.exe render case.nbproj -o out.mp4 --audit
```
종료 코드: 0 성공 · 3 재검사 노출 · 4 모델 라이선스/해시 오류. 노출이 나오면 `rules case.nbproj --mask-exposures` 후 다시 render.

## 테스트
```bash
.venv/Scripts/python.exe -m pytest -q -m "not slow and not ui"
```
```bash
.venv/Scripts/python.exe -m pytest -q
```
`tests/test_exposure_audit.py`(노출 재검사 게이트)가 깨지면 머지 불가. 벤치마크: `python scripts/bench.py tests/data/synthetic/street_faces.mp4 --interval 2 --protect-gt 1`.

## 현재 한계 (자세히는 docs/10 열린 질문)
- 번호판: 국내 모델 학습 전이라 차량 박스 하단 영역을 보수적으로 마스킹한다(과마스킹).
- 얼굴 신원 매칭(참조 사진)·번호판 OCR은 V1.
- 배포본은 LGPL FFmpeg로 빌드한 PyAV가 필요하다(`scripts/ci/check_license.py --packaging`).
