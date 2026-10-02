# 00. 인계 안내 — 무엇을 어떤 순서로 만들 것인가

## 한 줄 요약
워커(CLI)를 먼저 완성해 노출 재검사가 통과한 뒤 UI를 얹고, 마지막에 기관 모드(결재·리포트)를 붙인다.

## 산출물 패키지
| 파일 | 내용 |
|---|---|
| `CLAUDE.md` | Claude Code 프로젝트 지침·절대 규칙 |
| `docs/01-PRD.md` | 제품 요구사항 (F-01~F-19, 비기능, 시나리오) |
| `docs/02-ARCHITECTURE.md` | 3계층 구조, 프로세스 모델, 런타임 선택 |
| `docs/03-PIPELINE-SPEC.md` | 분석/렌더링 패스 단계별 스펙, 파라미터 기본값, 이벤트 |
| `docs/04-DATA-MODEL.md` | SQLite 스키마, .nbproj 포맷, 기관 테이블 |
| `docs/05-API.md` + `proto/nuriblur.proto` | gRPC 계약 |
| `docs/06-UI-SPEC.md` | 6단계 화면·상태 머신·컴포넌트 (목업 기준) |
| `docs/07-KR-PLATE-TRAINING.md` | 국내 번호판 검출·OCR 학습 계획 |
| `docs/08-ORG-MODE.md` | 기관 모드: 결재선 설정·감사 로그·보고서 |
| `docs/09-LICENSE-COMPLIANCE.md` | 허용/금지 구성요소, CI 차단 규칙 |
| `docs/10-ROADMAP-TASKS.md` | G0~G5 작업 체크리스트 (PR ID) |
| `mockup/nuriblur-mockup.html` | 단일 HTML 목업 v0.3 (6단계, 결재선 설정 포함) |

## 개발 순서 (게이트)
1. **G0 뼈대**: 저장소 구조, `proto` 확정, `models/manifest.json` 스키마, CI(라이선스 검사·lint·pytest).
2. **G1 워커 CLI**: `nuriblur probe`, `nuriblur analyze in.mp4 -o job.nbproj`, `nuriblur render job.nbproj --protect 3,7 -o out.mp4`. 공개 얼굴 모델 + 번호판 1차 모델로 동작. **노출 재검사 테스트 통과가 G1 완료 기준.**
3. **G2 UI 1~5단계**: 목업 그대로. 워커 이벤트 구독, 트랙 오버레이 클릭, 타임라인, 내보내기.
4. **G3 기관 모드**: 결재선 설정, 결재 흐름, 감사 로그, 보고서 PDF, 폐쇄망 설치본. → MVP(기관 PoC) 완료.
5. **G4 V1**: 번호판 2차 파인튜닝, ArcFace 참조 매칭, 검수 타임라인 고도화, GS인증 준비.
6. **G5 V2**: SAM 2 세그먼트, 배치/헤드리스, macOS, B2C 간편 모드.

## 완료 기준 요약 (MVP)
- 1080p 10분 영상, RTX 4060: 분석 ≤5분, 렌더링 ≤2분
- 얼굴 누락률 <3%, 번호판 누락률 <5% (회귀 세트 기준)
- 노출 재검사 0건, 기관 1곳 PoC 통과

## 첫 세션에서 Claude Code에게 줄 프롬프트
```
CLAUDE.md와 docs/00~10을 읽고 G0 뼈대를 만들어라.
- proto/nuriblur.proto로 python gRPC 코드를 생성하는 Makefile 타깃
- models/manifest.json 스키마 + 검증 스크립트 (AGPL/비상업 모델 차단)
- tests/test_exposure_audit.py 뼈대(실패하는 테스트로 시작)
- scripts/bench.py 뼈대
완료 후 docs/10-ROADMAP-TASKS.md의 G0 항목을 체크하고 PR 설명을 써라.
```
