"""GT 초안 생성 — 실제 영상(CCTV 등) 회귀 세트용.

  python scripts/make_gt.py tests/data/cctv_street/clip.mp4 [--min-face 0.5] [--every 1]

제품 프로파일보다 강한 설정(YOLOX-s, 얼굴 원해상도 타일 1920, 모든 프레임 검출)으로 얼굴·번호판을 찾아
{clip}.gt.json 을 만든다. 신뢰도 높은 검출만 GT로 쓴다(얼굴 ≥0.5, 번호판 보정점수 ≥0.5).
- `"verified": false` 로 저장된다. 사람이 대조 시트로 확인·수정한 뒤 true로 바꾼다.
- 초안 GT 대비 누락률 = '빠른 제품 설정이 정밀 설정이 확실히 본 얼굴을 얼마나 가렸나'(일관성 지표).
  정밀 설정도 못 본 얼굴은 잡지 못하므로 최종 품질 지표는 검수된 GT로 잰다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--min-face", type=float, default=0.5)
    ap.add_argument("--min-plate", type=float, default=0.5)
    ap.add_argument("--every", type=int, default=1, help="GT 프레임 간격")
    ap.add_argument("--long-side", type=int, default=1920)
    a = ap.parse_args()

    import numpy as np

    from worker.models.registry import Profile, default_registry
    from worker.pipeline.decode import iter_frames
    from worker.pipeline.detect import DetectorSet
    from worker.pipeline.probe import probe
    from worker.pipeline.track import ByteTracker

    reg = default_registry()
    base = reg.profile("gpu_precise")
    prof = Profile(name="gt", object=base.object, face=base.face, plate=base.plate, detect_interval=1,
                   face_long_side=a.long_side, plate_fallback="", plate_every=1)
    det = DetectorSet(prof, ("face", "plate"), reg)
    m = probe(a.video, with_hash=False)
    trackers = {c: ByteTracker(1, m["fps"] or 30) for c in ("face", "plate")}
    objs: dict[tuple[str, int], dict] = {}
    thr = {"face": a.min_face, "plate": a.min_plate}
    for fr in iter_frames(a.video):
        if fr.index % a.every:
            continue
        dets = det(fr.bgr)
        for c in ("face", "plate"):
            cd = [d for d in dets if d.cls == c]
            arr = np.array([[d.x1, d.y1, d.x2, d.y2, d.conf] for d in cd]).reshape(-1, 5)
            for di, tid in trackers[c].update(fr.index, arr):
                d = cd[di]
                if d.conf < thr[c]:
                    continue
                o = objs.setdefault((c, tid), {"id": len(objs) + 1, "cls": c, "boxes": {}})
                o["boxes"][str(fr.index)] = [round(d.x1, 1), round(d.y1, 1), round(d.x2 - d.x1, 1), round(d.y2 - d.y1, 1)]
        if fr.index % 50 == 0:
            print(f"  f{fr.index}/{m['frames']} 얼굴 {sum(1 for k in objs if k[0] == 'face')} 번호판 {sum(1 for k in objs if k[0] == 'plate')}",
                  flush=True)
    gt = {"fps": m["fps"], "frames": m["frames"], "width": m["width"], "height": m["height"], "verified": False,
          "generator": f"make_gt.py (YOLOX-s, face {a.long_side}, every {a.every}, face≥{a.min_face}, plate≥{a.min_plate})",
          "protected_ids": [], "objects": [o for o in objs.values() if len(o["boxes"]) >= 2]}
    out = Path(a.video).with_suffix(".gt.json")
    out.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    print(f"GT 초안: {out} (얼굴 {sum(o['cls'] == 'face' for o in gt['objects'])}, 번호판 {sum(o['cls'] == 'plate' for o in gt['objects'])} 객체)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
