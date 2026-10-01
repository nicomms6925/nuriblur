# 05. API 계약 (gRPC)

전체 정의는 `proto/nuriblur.proto`. 생성 코드는 `make proto` → `worker/pb/`, `app/pb/`.

| RPC | 용도 | 호출 시점 |
|---|---|---|
| Probe | 메타 조회 | 파일 추가 직후 |
| Analyze (stream) | 분석 패스, 이벤트 스트림 | 1단계 "분석 시작" |
| ListTracks | 트랙·썸네일·병합 제안 | 3·4단계 진입, 프레임 이동 시 `at_frame` |
| ApplyRules | 규칙 → 판정 | 보호대상 변경마다(수십 ms, 로컬 SQLite) |
| MergeTracks | 끊긴 트랙 병합 | 검수 "병합" |
| MatchReference (V1) | 참조 사진 임베딩·매칭 | 참조 사진 등록 |
| Render (stream) | 렌더링 패스 + 재검사 | 5단계 "내보내기 시작" |
| AuditCheck | 재검사 단독 실행 | 기관 모드 재검증 |
| Control | pause/resume/cancel | 2단계 버튼 |
| GetFrame | 단일 프레임(원본/마스킹) | 검수 스크러빙, 전/후 비교 |

## 보안
- 바인딩 127.0.0.1, 포트는 UI가 할당해 워커 실행 인자로 전달.
- 모든 호출 메타데이터 `x-nb-token`(UI 시작 시 생성, 32바이트 랜덤). 불일치 시 `UNAUTHENTICATED`.
- 파일 경로는 UI가 승인한 작업 폴더 하위만 허용(경로 탈출 검사).

## CLI (헤드리스, 같은 워커 코드 사용)
```
nuriblur probe in.mp4
nuriblur analyze in.mp4 -o case.nbproj [--profile gpu_precise] [--interval 2]
nuriblur rules case.nbproj --protect 3,7 --plate 12가3456 --ref ref1.jpg
nuriblur render case.nbproj -o out.mp4 --style pixelate --audit
nuriblur audit case.nbproj out.mp4
```
CLI 종료 코드: 0 성공, 3 재검사 노출 있음, 4 모델 라이선스/해시 오류.

## G1 구현 시 추가·확정 사항 (하위 호환 확장)
| 항목 | 내용 |
|---|---|
| `AllowRoot(AllowRootRequest) → Ack` | UI가 사용자가 고른 파일의 폴더를 등록한다. 등록된 폴더 하위가 아니면 모든 경로 RPC가 `PERMISSION_DENIED` |
| `Health(Empty) → WorkerInfo` | 기동 확인. `ep`(CUDA/DirectML/CoreML/CPU)·`providers`·사용 가능한 LGPL 인코더 목록 |
| `ApplyRulesRequest.keep_stored`, `.actor` | `keep_stored=true`면 `rules`를 무시하고 저장된 규칙으로 판정만 재계산(프로젝트 다시 열기) |
| `DecisionList.rules` | 저장된 규칙(id 확정)을 함께 돌려준다 |
| `FrameRequest.profile` | 마스킹 미리보기 스타일. 비우면 마지막 렌더 프로파일 |
| `RenderProfile.keep_audio`, `.no_head_fallback` | 오디오 유지(proto3 기본값 false이므로 UI는 항상 설정), 얼굴 없는 전신 머리 마스킹 끄기 |
| `AnalyzeRequest.resume_from_frame = -1` | 자동 재개: 같은 원본(SHA-256)의 미완료 프로젝트면 체크포인트부터, 완료된 프로젝트면 재분석 없이 `done` |
| 이벤트 `log` | 정보 로그(UI 로그 뷰) |
| 이벤트 `exposure` | 재검사 노출 1건(`frame`, `code`=face/mask_missing, `message`=JSON 박스). 최대 200건, `done` 앞에 온다 |
| `done` 순서 | 작업 스레드가 프로젝트를 닫고 잠금을 푼 **뒤**에 보낸다(직후 ListTracks가 `E_BUSY`가 되지 않도록) |
| `ListTracks(at_frame ≥ 0)` | 그 프레임에 있는 트랙만, 박스는 그 프레임 1개. `at_frame < 0`이면 전체 트랙(박스 없음)+병합 제안 |
| `MatchReference`, `SegmentTrack` | `UNIMPLEMENTED` (V1/V2) |
| 오류 코드 추가 | `E_BUSY`(같은 프로젝트 작업 중), `E_PATH`(경로 거부) |
| `TrackObject(TrackObjectRequest) → TrackObjectResult` | [G4-04] 검수: 사용자가 한 프레임에서 드래그한 객체(face/plate/other)를 앞뒤로 자동 추적(해당 검출기 + 템플릿 매칭). UI는 결과 박스로 `manual_box` 규칙을 만든다 |
| 이벤트 `warning W_FAST_MOTION` | 적응형 검출 간격: 빠른 움직임(타임랩스 등)으로 매 프레임 검출로 전환 |
