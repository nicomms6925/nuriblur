"""link · 병합 제안 (G1-05).

- 얼굴↔전신: 얼굴 박스가 전신 박스 상단 40% 안에 IoA ≥0.7 이고, 그런 프레임이 얼굴 트랙의 60% 이상.
- 병합 제안: 끊긴 구간 ≤2초, 외형 코사인 ≥0.5, 마지막/첫 박스 중심 거리 ≤ 박스 폭×2.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

import numpy as np

from worker.io.project import TrackRow
from worker.pipeline.identify import cosine

LINK_IOA = 0.7
LINK_TIME = 0.6
MERGE_GAP_S = 2.0
MERGE_COS = 0.5
MERGE_DIST = 2.0


def _ioa_top(face: tuple, person: tuple) -> float:
    fx, fy, fw, fh = face[:4]
    px, py, pw, ph = person[:4]
    tx1, ty1, tx2, ty2 = px, py, px + pw, py + ph * 0.4
    ix = max(0.0, min(fx + fw, tx2) - max(fx, tx1))
    iy = max(0.0, min(fy + fh, ty2) - max(fy, ty1))
    return ix * iy / max(fw * fh, 1e-6)


def link_faces_to_persons(faces: list[TrackRow], persons: list[TrackRow]) -> dict[int, int]:
    """face_id -> person_id"""
    out: dict[int, int] = {}
    # 프레임별 전신 박스 색인 — 얼굴 박스마다 그 프레임의 전신만 본다 (군중 영상의 얼굴×전신 전수 비교 회피)
    at: dict[int, list[tuple[int, tuple]]] = {}
    for p in persons:
        for fr, pb in p.boxes.items():
            at.setdefault(fr, []).append((p.id, pb))
    for f in faces:
        if not f.boxes:
            continue
        n: dict[int, int] = {}
        for fr, fb in f.boxes.items():
            for pid, pb in at.get(fr, ()):
                if _ioa_top(fb, pb) >= LINK_IOA:
                    n[pid] = n.get(pid, 0) + 1
        if n:
            best = max(n, key=lambda k: (n[k], -k))
            if n[best] / len(f.boxes) >= LINK_TIME:
                out[f.id] = best
    return out


@dataclass
class Suggestion:
    from_id: int
    to_id: int
    similarity: float
    reason: str


def merge_suggestions(tracks: list[TrackRow], fps: float) -> list[Suggestion]:
    max_gap = int(MERGE_GAP_S * (fps or 30))
    alive = [t for t in tracks if t.merged_into is None and t.boxes]
    out: list[Suggestion] = []
    by_cls: dict[str, list[TrackRow]] = {}
    for t in alive:
        by_cls.setdefault(t.cls, []).append(t)
    for group in by_cls.values():
        # 끝 프레임 순으로 정렬해 b 시작 직전 max_gap 안에서 끝난 트랙만 본다 (군중 영상의 수천 트랙에서 O(n²) 회피)
        group.sort(key=lambda t: t.end_f)
        ends = [t.end_f for t in group]
        la = np.array([t.boxes[t.end_f] if t.end_f in t.boxes else t.boxes[max(t.boxes)] for t in group],
                      np.float64)[:, :4].reshape(-1, 4)
        lcx, lcy, lim = la[:, 0] + la[:, 2] / 2, la[:, 1] + la[:, 3] / 2, MERGE_DIST * np.maximum(la[:, 2], 1.0)
        for b in group:
            fb = b.boxes[b.start_f] if b.start_f in b.boxes else b.boxes[min(b.boxes)]
            lo, hi = bisect_left(ends, b.start_f - max_gap), bisect_left(ends, b.start_f)
            if lo >= hi:
                continue
            d = np.hypot(lcx[lo:hi] - (fb[0] + fb[2] / 2), lcy[lo:hi] - (fb[1] + fb[3] / 2))
            cands = []
            for i in np.flatnonzero(d <= lim[lo:hi]):
                a = group[lo + int(i)]
                sim = cosine(a.embedding, b.embedding)
                if sim >= MERGE_COS:
                    cands.append((sim, a, b.start_f - a.end_f))
            if cands:
                sim, a, gap = max(cands, key=lambda x: x[0])
                out.append(Suggestion(b.id, a.id, sim, f"gap={gap}f"))
    return out
