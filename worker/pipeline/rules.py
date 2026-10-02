"""rules.apply (G1-07) — protect_rule → track_decision, deny-by-default.

- 모든 트랙은 기본 마스킹(protected=0).
- click {"track_id": N, "protect": true}            → 해당 트랙 보호(신뢰도 1.0)
- plate_text {"plates": [...], "max_edit": 1}       → OCR 텍스트 일치 시 보호, OCR 신뢰도 <0.8 이면 REVIEW(마스킹 유지)
- ref_face                                          → 신원 임베딩 모델(G4-02) 전까지 판정 불가 → 무시
- region / timerange / manual_box                   → 트랙 판정이 아니라 렌더링 계획(mask.py)에서 처리
- 승계: 병합된 트랙은 병합 대상의 판정을, 보호된 얼굴과 연결된 전신은 보호를 승계(양방향).
- 임계 미만 보호 후보는 보호하지 않고 REVIEW. 보호 트랙과 병합 제안으로 이어진 트랙도 REVIEW.
"""
from __future__ import annotations

from typing import Any

from worker.io.project import Project, TrackRow
from worker.pipeline.link import merge_suggestions

PLATE_OCR_MIN = 0.8
LINK_CONFIDENCE = 0.9
DEFAULT_THRESHOLD = 0.55


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _norm_plate(s: str) -> str:
    return "".join(ch for ch in s if not ch.isspace() and ch != "-")


def root_of(tid: int, by_id: dict[int, TrackRow]) -> int:
    seen = set()
    while by_id.get(tid) and by_id[tid].merged_into and tid not in seen:
        seen.add(tid)
        tid = by_id[tid].merged_into  # type: ignore[assignment]
    return tid


def compute(tracks: list[TrackRow], rules: list[dict[str, Any]], fps: float = 30.0,
            threshold: float = DEFAULT_THRESHOLD, suggestions: list | None = None) -> list[dict[str, Any]]:
    by_id = {t.id: t for t in tracks}
    dec: dict[int, dict[str, Any]] = {
        t.id: {"track_id": t.id, "protected": False, "source_rule_id": None, "confidence": 0.0, "flag": "OK"}
        for t in tracks}

    def protect(tid: int, rule_id: int | None, conf: float) -> None:
        d = dec[tid]
        if conf >= threshold:
            if not d["protected"] or conf > d["confidence"]:
                d.update(protected=True, source_rule_id=rule_id, confidence=conf, flag="OK")
        elif not d["protected"]:
            d.update(source_rule_id=rule_id, confidence=max(conf, d["confidence"]), flag="REVIEW")

    unprotect: set[int] = set()
    for r in rules:
        kind, p, rid = r["kind"], r.get("payload") or {}, r.get("id")
        if kind == "click":
            tid = int(p.get("track_id", -1))
            if tid not in by_id:
                continue
            root = root_of(tid, by_id)
            if p.get("protect", True):
                protect(root, rid, float(p.get("confidence", 1.0)))
            else:
                unprotect.add(root)
        elif kind == "plate_text":
            wanted = [_norm_plate(x) for x in p.get("plates", []) if x]
            max_edit = int(p.get("max_edit", 1))
            for t in tracks:
                if t.cls != "plate" or not t.plate_text:
                    continue
                txt = _norm_plate(t.plate_text)
                if any(edit_distance(txt, w) <= max_edit for w in wanted):
                    ocr = float(t.plate_conf or 0.0)
                    if ocr >= PLATE_OCR_MIN:
                        protect(root_of(t.id, by_id), rid, ocr)
                    else:
                        dec[t.id].update(source_rule_id=rid, confidence=ocr, flag="REVIEW")

    for tid in unprotect:  # 명시적 해제가 우선
        dec[tid].update(protected=False, source_rule_id=None, confidence=0.0, flag="OK")

    # 병합 승계: 루트 판정을 자식에게
    for t in tracks:
        root = root_of(t.id, by_id)
        if root != t.id:
            dec[t.id].update({k: v for k, v in dec[root].items() if k != "track_id"})

    # 얼굴↔전신 승계 (양방향)
    for t in tracks:
        if t.cls in ("face", "plate") and t.linked_person_id in dec:  # 얼굴↔전신, 번호판↔차량
            pid = t.linked_person_id
            if dec[t.id]["protected"] and not dec[pid]["protected"]:
                protect(pid, dec[t.id]["source_rule_id"], min(dec[t.id]["confidence"], LINK_CONFIDENCE))
            elif dec[pid]["protected"] and not dec[t.id]["protected"]:
                protect(t.id, dec[pid]["source_rule_id"], min(dec[pid]["confidence"], LINK_CONFIDENCE))

    # 보호 트랙으로 이어지는 병합 제안 → 검수 필요(마스킹 유지)
    sugg = suggestions if suggestions is not None else merge_suggestions(tracks, fps)
    for s in sugg:
        if s.to_id in dec and s.from_id in dec and dec[s.to_id]["protected"] and not dec[s.from_id]["protected"]:
            dec[s.from_id]["flag"] = "REVIEW"
            dec[s.from_id]["confidence"] = max(dec[s.from_id]["confidence"], s.similarity)

    return list(dec.values())


def apply(project: Project, rules: list[dict[str, Any]] | None = None, threshold: float = DEFAULT_THRESHOLD,
          actor: str = "") -> list[dict[str, Any]]:
    """규칙을 저장(주어진 경우)하고 판정을 계산·저장한다."""
    if rules is not None:
        project.replace_rules(rules, actor=actor)
    stored = project.rules()
    tracks = project.tracks(with_boxes=True)
    fps = project.media().get("fps") or 30.0
    decisions = compute(tracks, stored, fps=fps, threshold=threshold)
    project.write_decisions(decisions)
    return decisions


def exposure_rules(exposures: list[dict[str, Any]], span: int = 2, grow: float = 1.2) -> list[dict[str, Any]]:
    """노출 재검사 결과 → 수동 박스 규칙 (검수 '노출 영역 마스킹' 동작).

    재검사는 stride 간격으로만 프레임을 보므로 앞뒤 span 프레임까지 같은 박스로 덮는다.
    """
    out = []
    for e in exposures:
        if e.get("cls") == "frame_count":
            continue
        cx, cy = e["x"] + e["w"] / 2, e["y"] + e["h"] / 2
        w, h = e["w"] * grow, e["h"] * grow
        box = [cx - w / 2, cy - h / 2, w, h]
        f = int(e["frame"])
        out.append({"kind": "manual_box", "payload": {
            "frames": [[max(0, f - span), *box], [f + span, *box]], "cls": e.get("cls", "face"),
            "source": "audit"}})
    return out

