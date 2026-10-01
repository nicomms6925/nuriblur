"""노출 재검사 게이트 (CLAUDE.md 절대 규칙) — 이 테스트가 깨지면 머지 불가.

1. 회귀 클립: 분석 → 보호대상 지정 → 렌더링 → 재검사 노출 0건, GT 얼굴 누락률 < 3%
2. 재검사 민감도: 실제 노출(마스크 없음 / 분석 누락 / 마스크 어긋남 / 마스크 빠짐)을 반드시 잡아야 한다
"""
from __future__ import annotations

import pytest

from worker.io.project import Project
from worker.jobs import JobControl
from worker.pipeline import rules as rules_mod
from worker.pipeline.audit_check import audit
from worker.pipeline.decode import iter_frames
from worker.pipeline.encode import VideoWriter
from worker.pipeline.mask import MaskPlan, Region, RenderProfile, apply_masks, plan_for_project
from worker.pipeline.render import render

pytestmark = pytest.mark.slow

MAX_FACE_MISS = 0.03


def _protect_actor(path, gt, actor=1):
    from bench import protect_gt_tracks

    with Project.open(path) as p:
        ids = protect_gt_tracks(p, gt, {actor})
        rules_mod.apply(p, [{"kind": "click", "payload": {"track_id": t}} for t in ids])
        p.save()
    return ids


def test_regression_zero_exposure_and_miss_rate(project_copy, gt, tmp_path, events):
    from bench import face_miss_rate

    protected = _protect_actor(project_copy, gt, 1)
    assert protected, "보호대상 트랙을 찾지 못함"
    out = tmp_path / "out.mp4"
    r = render(project_copy, out, {}, "t-render", JobControl("t"), events, run_audit=True)
    assert r["audited"]
    assert r["exposures"] == [], f"노출 재검사 실패: {r['exposures'][:5]}"
    assert r["frames"] == gt["frames"]
    done = events.of("done")[-1]
    assert done["audit_exposures"] == 0 and done["output_sha256"] == r["sha256"]
    with Project.open(project_copy) as p:
        plan = plan_for_project(p, RenderProfile())
        job = p.jobs("render")[-1]
    assert job["status"] == "AUDITED"
    m = face_miss_rate(plan, gt, {1})
    assert m["miss_rate"] < MAX_FACE_MISS, m


def test_protected_face_left_visible(project_copy, gt):
    """보호대상 얼굴은 마스킹되지 않아야 한다 (deny-by-default의 반대편)."""
    from worker.pipeline.mask import coverage

    _protect_actor(project_copy, gt, 1)
    with Project.open(project_copy) as p:
        plan = plan_for_project(p, RenderProfile())
    actor1 = next(o for o in gt["objects"] if o["id"] == 1)
    covered = sum(coverage((x, y, x + w, y + h), plan.at(int(f))) >= 0.9 for f, (x, y, w, h) in actor1["boxes"].items())
    assert covered / len(actor1["boxes"]) < 0.2


def _render_with_plan(src, media, plan, out):
    w = VideoWriter(out, media, keep_audio=False)
    for fr in iter_frames(src):
        w.write(apply_masks(fr.bgr, plan.at(fr.index), RenderProfile()), fr.pts)
    w.close()


def test_audit_catches_unmasked_source(street_clip):
    ex = audit(street_clip, MaskPlan(), stride=10)
    assert len(ex) >= 10


def test_audit_catches_missing_track(project_copy, street_clip, tmp_path):
    with Project.open(project_copy) as p:
        media = p.media()
        plan = plan_for_project(p, RenderProfile())
        # GT 3번 배우(중간 등장)를 덮는 트랙 전부를 계획에서 제거 = 분석 누락 시뮬레이션
        drop = {t.id for t in p.tracks(with_boxes=True) if t.cls == "face" and 54 <= t.start_f <= 60}
    assert drop
    plan2 = MaskPlan(regions={f: [r for r in rs if r.track_id not in drop] for f, rs in plan.regions.items()})
    out = tmp_path / "miss.mp4"
    _render_with_plan(street_clip, media, plan2, out)
    ex = audit(out, plan2, stride=2, src_path=street_clip, profile=RenderProfile())
    assert any(e["cls"] == "face" for e in ex)


def test_audit_catches_shifted_masks(project_copy, street_clip, tmp_path):
    with Project.open(project_copy) as p:
        media = p.media()
        plan = plan_for_project(p, RenderProfile())

    def shift(r: Region) -> Region:
        h = r.y2 - r.y1
        return Region(r.x1, r.y1 - h * 0.55, r.x2, r.y2 - h * 0.55, r.kind, r.track_id)

    plan3 = MaskPlan(regions={f: [shift(r) for r in rs] for f, rs in plan.regions.items()})
    out = tmp_path / "shift.mp4"
    _render_with_plan(street_clip, media, plan3, out)
    ex = audit(out, plan3, stride=2, src_path=street_clip, profile=RenderProfile())
    assert len({e["frame"] for e in ex}) >= 20


def test_audit_integrity_catches_missing_masks(project_copy, street_clip):
    """출력에 마스크가 실제로 들어가지 않았다면(원본 그대로) mask_missing 으로 잡는다."""
    with Project.open(project_copy) as p:
        plan = plan_for_project(p, RenderProfile())
    ex = audit(street_clip, plan, stride=30, src_path=street_clip, profile=RenderProfile())
    assert any(e["cls"] == "mask_missing" for e in ex)


def test_exposure_review_flow_converges(project_copy, tmp_path):
    """재검사 노출 → 검수 '노출 영역 마스킹' → 재렌더링을 반복하면 3회 안에 0건이 되어야 한다
    (검출기 잡음성 저신뢰 히트도 검수자가 이 동작으로 정리한다)."""
    with Project.open(project_copy) as p:
        rules_mod.apply(p, rules_mod.exposure_rules(
            [{"frame": 30, "cls": "face", "x": 600, "y": 100, "w": 40, "h": 50, "conf": 0.3}]))
        p.save()
        plan = plan_for_project(p, RenderProfile())
    assert any(r.kind == "manual" for f in (28, 30, 32) for r in plan.at(f))
    history = []
    for i in range(3):
        r = render(project_copy, tmp_path / f"o{i}.mp4", {}, "t", JobControl("t"), lambda e: None, run_audit=True)
        history.append(len(r["exposures"]))
        if not r["exposures"]:
            break
        with Project.open(project_copy) as p:
            stored = [dict(id=x["id"], kind=x["kind"], payload=x["payload"]) for x in p.rules()]
            rules_mod.apply(p, stored + rules_mod.exposure_rules(r["exposures"], span=2))
            p.save()
    assert history[-1] == 0, history


def test_audit_catches_unmasked_plate(street_clip, gt):
    """번호판 모델(audit_capable)로 가리지 않은 번호판을 노출로 잡는다."""
    ex = audit(street_clip, MaskPlan(), stride=10, plate_model="plate_rtdetr_openimages")
    plates = [e for e in ex if e["cls"] == "plate"]
    assert plates
    g = next(o for o in gt["objects"] if o["cls"] == "plate")["boxes"]
    e = plates[0]
    x, y, w, h = g[str(e["frame"])]
    assert abs(e["x"] - x) < 10 and abs(e["y"] - y) < 10


def test_plate_masked_in_regression(project_copy, gt):
    from bench import face_miss_rate

    with Project.open(project_copy) as p:
        plan = plan_for_project(p, RenderProfile())
    assert face_miss_rate(plan, gt, set(), cls="plate")["miss_rate"] < 0.05
