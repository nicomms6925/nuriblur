"""link · 병합 제안 (G1-05).

- 얼굴↔전신: 얼굴 박스가 전신 박스 상단 40% 안에 IoA ≥0.7 이고, 그런 프레임이 얼굴 트랙의 60% 이상.
- 병합 제안: 끊긴 구간 ≤2초, 외형 코사인 ≥0.5, 마지막/첫 박스 중심 거리 ≤ 박스 폭×2.
"""
from __future__ import annotations

from dataclasses import dataclass

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
    for f in faces:
        if not f.boxes:
            continue
        best, best_n = None, 0
        for p in persons:
            if p.end_f < f.start_f or p.start_f > f.end_f:
                continue
            n = 0
            for fr, fb in f.boxes.items():
                pb = p.boxes.get(fr)
                if pb is not None and _ioa_top(fb, pb) >= LINK_IOA:
                    n += 1
            if n > best_n:
                best, best_n = p.id, n
        if best is not None and best_n / len(f.boxes) >= LINK_TIME:
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
        group.sort(key=lambda t: t.start_f)
        for b in group:
            cands = []
            for a in group:
                gap = b.start_f - a.end_f
                if a.id == b.id or gap <= 0 or gap > max_gap:
                    continue
                la = a.boxes[max(a.boxes)]
                fb = b.boxes[min(b.boxes)]
                dist = ((la[0] + la[2] / 2 - fb[0] - fb[2] / 2) ** 2 + (la[1] + la[3] / 2 - fb[1] - fb[3] / 2) ** 2) ** 0.5
                if dist > MERGE_DIST * max(la[2], 1.0):
                    continue
                sim = cosine(a.embedding, b.embedding)
                if sim >= MERGE_COS:
                    cands.append((sim, a, gap))
            if cands:
                sim, a, gap = max(cands, key=lambda x: x[0])
                out.append(Suggestion(b.id, a.id, sim, f"gap={gap}f"))
    return out
