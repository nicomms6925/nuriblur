import numpy as np

from worker.io.project import TrackRow
from worker.pipeline.identify import appearance
from worker.pipeline.rules import compute, edit_distance, exposure_rules


def _t(tid, cls="face", s=0, e=10, x=100, **kw):
    t = TrackRow(id=tid, cls=cls, start_f=s, end_f=e, **kw)
    t.boxes = {f: (x, 100, 40, 50, 0.9, 0) for f in range(s, e + 1)}
    return t


def _d(dec):
    return {d["track_id"]: d for d in dec}


def test_deny_by_default():
    d = _d(compute([_t(1), _t(2, "plate"), _t(3, "person")], []))
    assert not any(x["protected"] for x in d.values())


def test_click_protects_and_unprotect_wins():
    d = _d(compute([_t(1), _t(2)], [{"id": 1, "kind": "click", "payload": {"track_id": 1}}]))
    assert d[1]["protected"] and d[1]["confidence"] == 1.0 and not d[2]["protected"]
    d = _d(compute([_t(1)], [{"id": 1, "kind": "click", "payload": {"track_id": 1}},
                             {"id": 2, "kind": "click", "payload": {"track_id": 1, "protect": False}}]))
    assert not d[1]["protected"]


def test_low_confidence_protect_becomes_review_and_stays_masked():
    d = _d(compute([_t(1)], [{"id": 1, "kind": "click", "payload": {"track_id": 1, "confidence": 0.4}}]))
    assert not d[1]["protected"] and d[1]["flag"] == "REVIEW"


def test_merge_inherits_root_decision():
    tracks = [_t(1, s=0, e=10), _t(2, s=20, e=30, merged_into=1)]
    d = _d(compute(tracks, [{"id": 1, "kind": "click", "payload": {"track_id": 1}}]))
    assert d[2]["protected"]
    d = _d(compute(tracks, [{"id": 1, "kind": "click", "payload": {"track_id": 2}}]))
    assert d[1]["protected"] and d[2]["protected"]  # 자식 클릭 → 루트 보호 → 승계


def test_face_person_link_inheritance():
    tracks = [_t(1, linked_person_id=5), _t(5, "person")]
    d = _d(compute(tracks, [{"id": 1, "kind": "click", "payload": {"track_id": 1}}]))
    assert d[5]["protected"]
    d = _d(compute(tracks, [{"id": 1, "kind": "click", "payload": {"track_id": 5}}]))
    assert d[1]["protected"]


def test_plate_text_rule():
    tracks = [_t(1, "plate", plate_text="12가3456", plate_conf=0.95), _t(2, "plate", plate_text="12가3457", plate_conf=0.6),
              _t(3, "plate", plate_text="99나0000", plate_conf=0.99)]
    d = _d(compute(tracks, [{"id": 7, "kind": "plate_text", "payload": {"plates": ["12가 3456"], "max_edit": 1}}]))
    assert d[1]["protected"] and d[1]["source_rule_id"] == 7
    assert not d[2]["protected"] and d[2]["flag"] == "REVIEW"
    assert not d[3]["protected"]


def test_merge_suggestion_to_protected_flags_review():
    img = np.full((60, 50, 3), 120, np.uint8)
    emb = appearance(img).tobytes()
    a = _t(1, s=0, e=10, embedding=emb)
    b = _t(2, s=20, e=30, x=110, embedding=emb)
    d = _d(compute([a, b], [{"id": 1, "kind": "click", "payload": {"track_id": 1}}], fps=30))
    assert d[2]["flag"] == "REVIEW" and not d[2]["protected"]


def test_edit_distance():
    assert edit_distance("12가3456", "12가3456") == 0
    assert edit_distance("12가3456", "12나3456") == 1


def test_exposure_rules_span():
    r = exposure_rules([{"frame": 10, "cls": "face", "x": 0, "y": 0, "w": 10, "h": 10, "conf": 0.3}], span=2)
    assert r[0]["kind"] == "manual_box" and [f[0] for f in r[0]["payload"]["frames"]] == [8, 12]
