"""회귀 세트 실행 (G0-06 / G1 게이트) — 폴더의 모든 영상에 분석→렌더링→노출 재검사.

  python scripts/regress.py tests/data/synthetic [--profile cpu] [--out .scratch/regress] [--only 이름일부]

영상마다:
- 분석/렌더 시간·FPS, 트랙 수, 재검사 노출 수(얼굴·번호판·mask_missing)
- {clip}.gt.json 이 있으면 클래스별 누락률(마스크가 GT 박스를 90% 미만 덮는 비율)
- 대조 시트 {clip}_sheet.jpg: 균등 간격 6프레임의 원본|마스킹 — 재검사로는 알 수 없는
  '검출기가 아예 못 본 얼굴'을 사람이 눈으로 확인하는 용도
결과 요약: {out}/report.json, report.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v"}


def contact_sheet(src: Path, out: Path, sheet: Path, n: int = 6, width: int = 640) -> None:
    import cv2
    import numpy as np

    from worker.pipeline.decode import iter_frames

    total = sum(1 for _ in iter_frames(out))
    picks = {int(total * (i + 0.5) / n) for i in range(n)}
    a = {f.index: f.bgr for f in iter_frames(src) if f.index in picks}
    b = {f.index: f.bgr for f in iter_frames(out) if f.index in picks}
    rows = []
    for k in sorted(picks):
        if k in a and k in b:
            s = width / a[k].shape[1]
            pair = [cv2.resize(x, (width, int(x.shape[0] * s)), interpolation=cv2.INTER_AREA) for x in (a[k], b[k])]
            row = np.hstack(pair)
            cv2.putText(row, f"f{k}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            rows.append(row)
    if rows:
        cv2.imwrite(str(sheet), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])


def run_one(video: Path, outdir: Path, profile: str, interval: int) -> dict:
    from collections import Counter

    from bench import face_miss_rate

    from worker.io.project import Project
    from worker.jobs import JobControl
    from worker.pipeline.analyze import analyze
    from worker.pipeline.mask import RenderProfile, plan_for_project
    from worker.pipeline.render import render

    proj = outdir / f"{video.stem}.nbproj"
    out = outdir / f"{video.stem}_blur.mp4"
    proj.unlink(missing_ok=True)
    t0 = time.monotonic()
    stats = analyze(video, proj, "regress-a", JobControl("a"), lambda e: None, profile=profile, detect_interval=interval)
    t_an = time.monotonic() - t0
    t0 = time.monotonic()
    r = render(proj, out, {}, "regress-r", JobControl("r"), lambda e: None, run_audit=True)
    t_re = time.monotonic() - t0
    res = {"video": video.name, "frames": r["frames"], "analyze_s": round(t_an, 1),
           "analyze_fps": round(r["frames"] / t_an, 2), "render_fps": round(r["render_fps"], 1),
           "render_audit_s": round(t_re, 1), "encoder": r["encoder"],
           "tracks": {k: stats[k] for k in ("faces", "persons", "plates")}, "mask_tracks": r["mask_tracks"],
           "exposures": len(r["exposures"]), "exposure_by_class": dict(Counter(e["cls"] for e in r["exposures"])),
           "exposure_frames": sorted({e["frame"] for e in r["exposures"]})[:30]}
    gt_path = video.with_suffix(".gt.json")
    if gt_path.exists():
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        with Project.open(proj) as p:
            plan = plan_for_project(p, RenderProfile())
        for cls in ("face", "plate"):
            if any(o["cls"] == cls for o in gt["objects"]):
                m = face_miss_rate(plan, gt, set(gt.get("protected_ids", [])), cls=cls)
                res[f"{cls}_miss_rate"] = round(m["miss_rate"], 4)
    contact_sheet(video, out, outdir / f"{video.stem}_sheet.jpg")
    return res


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--profile", default="cpu")
    ap.add_argument("--interval", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / ".scratch" / "regress"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    videos = sorted(p for p in Path(a.folder).rglob("*") if p.suffix.lower() in VIDEO_EXT and a.only in p.name)
    results = []
    for v in videos:
        print(f"▶ {v.name}", flush=True)
        try:
            r = run_one(v, outdir, a.profile, a.interval)
        except Exception as e:  # noqa: BLE001
            r = {"video": v.name, "error": repr(e)}
        results.append(r)
        print("  " + json.dumps(r, ensure_ascii=False), flush=True)
        (outdir / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["| 영상 | 프레임 | 분석 FPS | 렌더 FPS | 얼굴/전신/번호판 트랙 | 재검사 노출 | 누락률(얼굴/번호판) |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['video']} | 오류 | {r['error'][:80]} |||||")
            continue
        t = r["tracks"]
        miss = f"{r.get('face_miss_rate', '-')} / {r.get('plate_miss_rate', '-')}"
        lines.append(f"| {r['video']} | {r['frames']} | {r['analyze_fps']} | {r['render_fps']} | "
                     f"{t['faces']}/{t['persons']}/{t['plates']} | {r['exposures']} {r['exposure_by_class'] or ''} | {miss} |")
    (outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if any(r.get("exposures") or "error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
