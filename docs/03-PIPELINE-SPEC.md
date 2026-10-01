# 03. 처리 파이프라인 스펙

## 한 줄 요약
분석 패스는 트랙 DB를 만들고, 렌더링 패스는 트랙 DB와 규칙만으로 출력한다. 설정을 바꿔도 분석은 다시 하지 않는다.

## 패스 1 — 분석 (Analyze)
| # | 단계 | 입력 → 출력 | 기본 파라미터 | 비고 |
|---|---|---|---|---|
| 1 | probe | 파일 → MediaInfo | — | VFR이면 `-vsync cfr`로 정규화 대상 표시, 회전 메타(90/180/270) 적용 |
| 2 | decode | 파일 → RGB 프레임 큐 | 링버퍼 64, HW 디코드 우선(NVDEC/QSV/D3D11VA) | HEVC 10-bit HW 미지원 시 SW 폴백 + warning 이벤트 |
| 3 | detect | 프레임 → 박스[cls, conf, xyxy] | 모델 YOLOX-s, 입력 1280(장변), conf 0.25(plate 0.25, face 0.30, person 0.35), NMS 0.5, 배치 8, **검출 간격 2프레임**(정적 카메라 4) | 사이 프레임은 추적기 예측 |
| 4 | track | 박스 시퀀스 → 트랙 | ByteTrack: high 0.5 / low 0.1, match 0.8, buffer 30프레임; 칼만 보간; 트랙 시작 전/종료 후 패딩 5프레임 | 클래스별 독립 추적 |
| 5 | identify | 얼굴 트랙 → 임베딩 / 번호판 트랙 → 텍스트 | 얼굴: 선명도 상위 3프레임 ArcFace(112×112), 평균 벡터. 번호판: 상위 5프레임 OCR, 정규식 통과분 다수결 | V1. MVP는 OCR만 선택적 |
| 6 | link | 얼굴↔전신 트랙 연결 | 얼굴 박스가 전신 박스 상단 40% 안에 IoA ≥0.7 & 시간 겹침 ≥60% | 보호 승계용 |
| 7 | save | 트랙 → .nbproj | SQLite + 트랙별 썸네일(96px) | checkpoint_frame 갱신 |

성능 목표: RTX 4060 1080p 분석 ≥60 FPS (검출 간격 2 포함).

## 패스 2 — 렌더링 (Render)
1. `rules.apply()` → `track_decision` (deny-by-default). 신뢰도 낮은 보호 후보는 `REVIEW` 플래그 + 마스킹 유지.
2. 프레임별 마스킹 영역 생성: 박스 ×1.25 확장, 좌표 EMA α=0.6, 패딩 프레임 포함, 전신 마스킹 옵션 시 전신 박스.
3. 마스킹 적용:
   | 스타일 | 파라미터 | 하한 |
   |---|---|---|
   | pixelate (기본) | block = w/8 | ≥ 8px |
   | gaussian | σ = w/4 | σ ≥ 15 |
   | solid | 검정 / 평균색 | — |
   | segment (V2) | SAM2 마스크 + feather 4px | — |
   번호판 크기 < 20px(장변)이면 차량 전체(전신에 해당하는 vehicle 박스 없음 → 번호판 박스 ×3)로 폴백.
4. 인코딩: 원본 코덱 유지, 비트레이트 = 원본 ×1.0(상한 원본 ×1.5), NVENC/QSV/VideoToolbox → 없으면 openh264/x264(LGPL 빌드 주의). `-c:a copy`, `-map_metadata -1`(메타 제거 기본 on), 타임스탬프 보존.
5. `audit_check`: 출력본을 검출기(conf 0.2)로 재검사, 보호 트랙 영역과 IoU<0.3인 얼굴/번호판 검출이 1건이라도 있으면 실패 → 노출 프레임 목록 반환.

## 이벤트 (워커 → UI)
```json
{"type":"progress","job":"j-…","stage":"detect","frame":4820,"total":18000,"fps":72.4,"eta_s":182,"gpu_util":0.81,"vram_mb":3120}
{"type":"stats","job":"j-…","faces":37,"persons":41,"plates":12,"protected":2,"review":3}
{"type":"preview","job":"j-…","frame":4820,"jpeg_b64":"…"}
{"type":"warning","job":"j-…","stage":"decode","code":"W_HWDEC_FALLBACK","message":"HEVC 10-bit HW 디코딩 미지원, SW 폴백"}
{"type":"error","job":"j-…","stage":"encode","code":"E_ENCODER","message":"NVENC 세션 한도 초과"}
{"type":"done","job":"j-…","stage":"render","output":"…/out.mp4","sha256":"…","audit":{"exposures":0}}
```
오류 코드: `E_CODEC, E_ENCODER, E_MODEL_HASH, E_MODEL_LICENSE, E_DISK, E_CANCELLED, E_AUDIT_EXPOSURE`.

## 제어
`Control(job, action)`: `pause | resume | cancel`. 분석 중단 시 처리된 트랙은 `checkpoint_frame`까지 보존, 재개 시 이어서.

## 트랙 병합 규칙 (ID 스위치)
- 자동 제안: 끊긴 구간 ≤ 2초, 임베딩 코사인 ≥ 0.5, 마지막/첫 박스 중심 거리 ≤ 박스 폭×2 → `merge_suggestion`
- 사용자가 승인하면 `track.merged_into` 설정, 보호 상태 승계.
