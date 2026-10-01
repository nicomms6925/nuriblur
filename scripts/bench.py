"""벤치마크 (G0-05 / G1-14) — 분석 FPS · 렌더 FPS · 얼굴 누락률 · 재검사 노출 수.

  python scripts/bench.py tests/data/synthetic/street_faces.mp4 [--profile cpu] [--interval 2] [--protect-gt 1]

GT(.gt.json)가 있으면 누락률을 계산한다:
  누락 = 보호대상이 아닌 GT 얼굴 박스 중, 렌더링 계획의 마스크 영역이 90% 미만 덮는 (프레임, 얼굴) 비율.
결과는 JSON으로 출력 — PR 본문에 그대로 붙인다(CLAUDE.md 작업 방식).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FACE_COVER = 0.9


def face_miss_rate(plan, gt: dict, protected_gt: set[int]) -> dict:
    from worker.pipeline.mask import coverage

    total = missed = 0
    per: dict[int, list[int]] = {}
    for o in gt["objects"]:
        if o["cls"] != "face" or o["id"] in protected_gt:
            continue
        for f, (x, y, w, h) in o["boxes"].items():
            total += 1
            c = coverage((x, y, x + w, y + h), plan.at(int(f)))
            if c < FACE_COVER:
                missed += 1
                per.setdefault(o["id"], []).append(int(f))
    return {"gt_face_frames": total, "missed": missed, "miss_rate": missed / total if total else 0.0,
            "missed_frames": {k: v[:20] for k, v in per.items()}}


def protect_gt_tracks(project, gt: dict, gt_ids: set[int]) -> list[int]:
    """GT 얼굴과 가장 많이 겹치는 얼굴 트랙을 클릭 규칙으로 보호 (테스트·벤치용)."""
    from worker.pipeline.audit_check import _iou

    tracks = project.tracks(with_boxes=True)
    chosen = []
    for o in gt["objects"]:
        if o["id"] not in gt_ids:
            continue
        score: dict[int, int] = {}
        for f, (x, y, w, h) in o["boxes"].items():
            for t in tracks:
                b = t.boxes.get(int(f))
                if t.cls == "face" and b and _iou((x, y, x + w, y + h), (b[0], b[1], b[0] + b[2], b[1] + b[3])) > 0.3:
                    score[t.id] = score.get(t.id, 0) + 1
        # 같은 사람에 대한 조각 트랙(가려짐 후 재등장)까지 모두 보호
        chosen += [tid for tid, n in score.items() if n >= 3]
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--profile", default="cpu")
    ap.add_argument("--interval", type=int, default=0)
    ap.add_argument("--protect-gt", default="", help="보호할 GT 객체 id (쉼표)")
    ap.add_argument("--out")
    a = ap.parse_args()

    from worker.io.project import Project
    from worker.jobs import JobControl
    from worker.models.registry import ep_label
    from worker.pipeline import rules as rules_mod
    from worker.pipeline.analyze import analyze
    from worker.pipeline.mask import RenderProfile, plan_for_project
    from worker.pipeline.render import render

    video = Path(a.video)
    gt_path = video.with_suffix(".gt.json")
    gt = json.loads(gt_path.read_text(encoding="utf-8")) if gt_path.exists() else None
    gt_ids = {int(x) for x in a.protect_gt.split(",") if x}
    tmp = Path(tempfile.mkdtemp(prefix="nbbench_"))
    proj = tmp / "bench.nbproj"
    out = Path(a.out) if a.out else tmp / "bench_out.mp4"

    def quiet(e):
        pass

    t0 = time.monotonic()
    stats = analyze(video, proj, "bench-a", JobControl("a"), quiet, profile=a.profile, detect_interval=a.interval)
    t_an = time.monotonic() - t0
    protected = []
    if gt and gt_ids:
        with Project.open(proj) as p:
            protected = protect_gt_tracks(p, gt, gt_ids)
            rules_mod.apply(p, [{"kind": "click", "payload": {"track_id": t}} for t in protected])
            p.save()
    t0 = time.monotonic()
    r = render(proj, out, {}, "bench-r", JobControl("r"), quiet, run_audit=True)
    t_all = time.monotonic() - t0
    res = {
        "video": str(video), "profile": a.profile, "ep": ep_label(), "cpu": platform.processor(),
        "frames": r["frames"], "analyze_s": round(t_an, 2), "analyze_fps": round(r["frames"] / t_an, 2),
        "render_fps": round(r["render_fps"], 2), "render_audit_s": round(t_all, 2), "encoder": r["encoder"],
        "tracks": {k: stats[k] for k in ("faces", "persons", "plates")}, "protected_tracks": protected,
        "audit_exposures": len(r["exposures"]),
    }
    if gt:
        with Project.open(proj) as p:
            plan = plan_for_project(p, RenderProfile())
        res["face"] = face_miss_rate(plan, gt, gt_ids)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
