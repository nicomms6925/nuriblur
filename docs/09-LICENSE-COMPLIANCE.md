# 09. 라이선스·컴플라이언스

## 한 줄 요약
상용 PC 배포 제품이므로 AGPL·비상업 모델은 쓰지 않는다. CI가 `models/manifest.json`과 의존성을 검사해 위반 시 빌드를 막는다.

## 허용 / 금지
| 구성요소 | 사용 | 라이선스 | 조치 |
|---|---|---|---|
| YOLOX (Megvii) | 검출 | Apache 2.0 | 자체 학습 가중치 |
| EgoBlur (Meta) | 검출 대안 | Apache 2.0 | 모델 크기 400MB 고려 |
| ByteTrack | 추적 | MIT | 자체 포팅(원 저장소 의존성 제거) |
| InsightFace 코드 | 얼굴 정렬 | MIT | **사전학습 가중치(buffalo 등) 금지** → 자체 학습 ArcFace |
| PaddleOCR | OCR | Apache 2.0 | 한글 사전 재학습 |
| SAM 2 / EdgeTAM | 세그먼트(V2) | Apache 2.0 | — |
| FFmpeg | 디코딩·인코딩 | LGPL 빌드만 | x264 등 GPL 컴포넌트 제외, HW 인코더/openh264 사용, 동적 링크·고지 |
| PySide6 | UI | LGPL | 동적 링크·고지 |
| ONNX Runtime | 추론 | MIT | — |
| yt-dlp | URL 다운로드(B2C) | Unlicense | 기관 설치본 제외 |
| **Ultralytics YOLOv5/8/11** | — | **AGPL-3.0** | **금지** (Enterprise 계약 없이는 import 자체 차단) |
| AI Hub 데이터 | 학습 | 개별 약관 | 모델 판매 가능·출처 고지, 데이터 재배포 금지 |

## CI 규칙 (`scripts/ci/check_license.py`)
- `pip-licenses`로 의존성 라이선스 수집, `AGPL|SSPL|Commons Clause` 포함 시 실패.
- `models/manifest.json`의 각 모델: `license ∈ {Apache-2.0, MIT, BSD, 자체}` 아니면 실패; `sha256` 불일치 시 실패.
- 소스에서 `import ultralytics` 발견 시 실패.
- 배포 패키지에 FFmpeg 빌드 설정(`--enable-gpl` 없음) 검증.

## 개인정보보호법·AI기본법
- 얼굴 임베딩·참조 사진은 프로젝트 내부 저장, 출력 제공 후 삭제 옵션(기본 on).
- 외부 전송 없음 고지, 네트워크 호출 3종 동의, 기관 설치본은 전부 비활성.
- 자동 검출 결과임을 표시하고 누락 가능성·검수 안내(AI기본법 투명성).
- 개인정보 영향평가 지원 자료: 처리 흐름도, 보관 기간, 접근 통제, 감사 로그 설명.

## 고지문 (About 화면·설치 문서)
오픈소스 목록·라이선스 전문, "본 제품의 번호판 인식 모델은 AI Hub(한국지능정보사회진흥원) 데이터를 활용하여 학습되었습니다."
