"""재검사 오탐 확인 ("노출 아님", G1-10) — 검수자가 확인한 재검사 검출을 통과로 인정한다.

정책 (docs/10 열린 질문 결정, 2026-10-02):
- 최근 재검사 결과(meta.last_audit)에 있는 항목만 확인할 수 있다(임의 영역 지정 불가).
- 얼굴·번호판 검출만 대상. 마스크 누락(mask_missing)·프레임 수 불일치(frame_count)는 출력 무결성 오류라 확인 불가.
- 신뢰도 DISMISS_MAX_CONF(0.5) 이상은 실제 얼굴·번호판일 가능성이 높아 확인 불가 — 마스킹해야 한다.
- 확인 내역(프레임·박스·종류·신뢰도·확인자·사유·시각·출력본 해시)은 프로젝트 meta.audit_dismissals에 쌓이고
  (기관 모드는 UI가 감사 로그에도 남긴다), 이후 재검사에서도 같은 프레임·같은 자리(IoU ≥ DISMISS_IOU)의
  같은 종류 검출은 노출로 세지 않는다(다시 렌더링해도 같은 오탐이 다시 나오므로).
- 남은 노출이 0이 되면 해당 렌더 작업을 AUDITED로 바꾼다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from worker.io.project import Project

DISMISS_MAX_CONF = 0.5
DISMISS_IOU = 0.5
DISMISSABLE = ("face", "plate")


def _iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def dismissals(project: Project) -> list[dict[str, Any]]:
    return json.loads(project.get_meta("audit_dismissals") or "[]")


class DismissIndex:
    """재검사 중 '확인된 오탐' 조회용 (프레임별)."""

    def __init__(self, items: list[dict[str, Any]] | None = None):
        self.by_frame: dict[int, list[dict[str, Any]]] = {}
        for d in items or []:
            self.by_frame.setdefault(int(d["frame"]), []).append(d)
        self.hits = 0

    def match(self, frame: int, cls: str, box: tuple[float, float, float, float]) -> bool:
        for d in self.by_frame.get(frame, ()):
            if d["cls"] == cls and _iou(box, (d["x"], d["y"], d["x"] + d["w"], d["y"] + d["h"])) >= DISMISS_IOU:
                self.hits += 1
                return True
        return False


def refusal(e: dict[str, Any]) -> str:
    """확인할 수 없는 이유 (빈 문자열이면 확인 가능)."""
    if e.get("cls") not in DISMISSABLE:
        return "integrity"
    if float(e.get("conf", 1.0)) >= DISMISS_MAX_CONF:
        return "high_conf"
    return ""


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (int(a["frame"]) == int(b["frame"]) and a.get("cls") == b.get("cls")
            and abs(float(a["x"]) - float(b["x"])) < 1 and abs(float(a["y"]) - float(b["y"])) < 1
            and abs(float(a["w"]) - float(b["w"])) < 1 and abs(float(a["h"]) - float(b["h"])) < 1)


def dismiss(project: Project, output_path: str | Path, items: list[dict[str, Any]], actor: str,
            reason: str) -> dict[str, Any]:
    """items: [{frame, cls, x, y, w, h}] — 최근 재검사 결과의 항목. 반환: accepted/refused/remaining."""
    if not reason.strip():
        raise ValueError("오탐 확인 사유가 필요합니다")
    last = json.loads(project.get_meta("last_audit") or "{}")
    if not last or Path(last.get("output", "")).resolve() != Path(output_path).resolve():
        raise ValueError("최근 재검사한 출력본이 아닙니다 — 다시 내보내기 후 확인하세요")
    exposures: list[dict[str, Any]] = last.get("exposures", [])
    total = int(last.get("count", len(exposures)))
    jobs = [j for j in project.jobs("render") if j.get("output_path") and
            Path(j["output_path"]).resolve() == Path(output_path).resolve()]
    sha = jobs[-1]["output_sha256"] if jobs else ""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    accepted, refused = [], []
    keep = list(exposures)
    for it in items:
        e = next((x for x in keep if _same(x, it)), None)
        if e is None:
            refused.append({**it, "why": "not_found"})
            continue
        why = refusal(e)
        if why:
            refused.append({**it, "why": why})
            continue
        keep.remove(e)
        accepted.append({"frame": int(e["frame"]), "cls": e["cls"], "x": e["x"], "y": e["y"], "w": e["w"],
                         "h": e["h"], "conf": e.get("conf", 0.0), "actor": actor, "reason": reason, "at": now,
                         "output_sha256": sha})
    remaining = max(0, total - len(accepted))
    if accepted:
        project.set_meta("audit_dismissals", json.dumps(dismissals(project) + accepted, ensure_ascii=False))
        project.set_meta("last_audit", json.dumps({**last, "exposures": keep, "count": remaining,
                                                   "dismissed": int(last.get("dismissed", 0)) + len(accepted)},
                                                  ensure_ascii=False))
        if jobs:
            project.upsert_job(jobs[-1]["id"], "render", audit_exposures=remaining,
                               status="AUDITED" if remaining == 0 else "REVIEWING")
        project.save()
    return {"accepted": accepted, "refused": refused, "remaining": remaining}
