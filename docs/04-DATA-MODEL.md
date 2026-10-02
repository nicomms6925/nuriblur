# 04. 데이터 모델 · .nbproj · 기관 DB

## .nbproj 포맷
ZIP 컨테이너: `project.sqlite` + `thumbs/{track_id}.jpg` + `manifest.json`(버전·생성 앱·모델 해시). 원본 영상은 포함하지 않고 경로·SHA-256만 저장.

## 스키마 (project.sqlite)
```sql
CREATE TABLE media (
  id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, codec TEXT, width INT, height INT,
  fps REAL, is_vfr INT, frames INT, duration_ms INT, rotation INT, audio_codec TEXT);

CREATE TABLE track (
  id INTEGER PRIMARY KEY, media_id INT, cls TEXT CHECK(cls IN ('face','person','plate','vehicle')),
  start_f INT, end_f INT, conf_avg REAL, embedding BLOB, plate_text TEXT, plate_conf REAL,
  linked_person_id INT, merged_into INT, thumb TEXT);
-- [추가] vehicle: 번호판 선택 시 차량 트랙. linked_person_id는 부모 트랙(얼굴→전신, 번호판→차량).
-- 구버전 프로젝트는 열 때 track 표를 다시 만들어 CHECK를 갱신한다(데이터 보존).

CREATE TABLE track_box (
  track_id INT, frame INT, x REAL, y REAL, w REAL, h REAL, conf REAL, interpolated INT,
  PRIMARY KEY(track_id, frame));

CREATE TABLE protect_rule (
  id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN ('click','ref_face','plate_text','region','timerange','manual_box')),
  payload TEXT, created_at TEXT, created_by TEXT);
-- payload 예: click {"track_id":3} / ref_face {"ref_ids":[1,2],"threshold":0.55}
--             plate_text {"plates":["12가3456"],"max_edit":1} / region {"polygon":[[x,y],…],"mode":"exclude"}
--             timerange {"start_ms":0,"end_ms":5000} / manual_box {"frames":[[f,x,y,w,h],[f,x,y,w,h]],"cls":"face"}

CREATE TABLE track_decision (
  track_id INT PRIMARY KEY, protected INT, source_rule_id INT, confidence REAL,
  flag TEXT CHECK(flag IN ('OK','REVIEW')), reviewed INT, reviewed_by TEXT, reviewed_at TEXT);

CREATE TABLE render_profile (
  id INTEGER PRIMARY KEY, style TEXT, strength REAL, pad_ratio REAL, pad_frames INT,
  codec TEXT, quality TEXT, strip_meta INT, watermark TEXT);

CREATE TABLE job (
  id TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('analyze','render')), status TEXT,
  checkpoint_frame INT, started_at TEXT, ended_at TEXT, stats TEXT, log_path TEXT,
  output_path TEXT, output_sha256 TEXT, audit_exposures INT);
```

## 기관 DB (org.sqlite, 설치 단위)
```sql
CREATE TABLE org_settings (key TEXT PRIMARY KEY, value TEXT); -- retention_days, watermark_default, offline_mode …

CREATE TABLE org_approval_line (            -- 기관 관리자가 설정 (2~4단)
  id INTEGER PRIMARY KEY, name TEXT, steps TEXT, -- JSON [{"role":"담당자 검수","user":"김형남"},{"role":"팀장","user":"박문화"},…]
  is_default INT, updated_by TEXT, updated_at TEXT);

CREATE TABLE case_file (                    -- 처리 건
  id TEXT PRIMARY KEY, receipt_no TEXT, legal_basis TEXT, requester_name TEXT,
  project_path TEXT, approval_line_snapshot TEXT,  -- 생성 시점 결재선 복사 (설정 변경 영향 없음)
  status TEXT, retention_until TEXT, created_by TEXT, created_at TEXT);

CREATE TABLE approval_step (
  id INTEGER PRIMARY KEY, case_id TEXT, step_no INT, role TEXT, user TEXT,
  decision TEXT CHECK(decision IN ('PENDING','APPROVED','REJECTED')), comment TEXT, decided_at TEXT);

CREATE TABLE audit_log (                    -- append-only; 삭제·수정 트리거로 금지
  id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, case_id TEXT, action TEXT, target TEXT, detail TEXT, prev_hash TEXT, hash TEXT);
-- hash = sha256(prev_hash || ts || actor || action || target || detail) 체인
```
- `audit_log`에는 `BEFORE UPDATE/DELETE` 트리거로 `RAISE(ABORT)`.
- 보관 만료(`retention_until`) 시 출력본·프로젝트 삭제, 감사 로그와 보고서 PDF는 보존.
