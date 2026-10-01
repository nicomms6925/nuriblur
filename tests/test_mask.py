import numpy as np
import pytest

from worker.io.project import TrackRow
from worker.pipeline.mask import (
    MIN_BLOCK,
    Region,
    RenderProfile,
    apply_masks,
    build_plan,
    coverage,
    interpolate_keyframes,
)


def _t(tid, cls="face", frames=range(10, 20), box=(100, 100, 40, 50), **kw):
    t = TrackRow(id=tid, cls=cls, start_f=min(frames), end_f=max(frames), **kw)
    t.boxes = {f: (*box, 0.9, 0) for f in frames}
    return t


def test_plan_masks_unprotected_and_pads():
    plan = build_plan([_t(1)], {}, [], RenderProfile(pad_frames=5), 100, 30)
    assert plan.at(5) and plan.at(24) and not plan.at(4) and not plan.at(25)
    r = plan.at(15)[0]
    assert r.x2 - r.x1 == pytest.approx(40 * 1.25)


def test_protected_not_masked():
    plan = build_plan([_t(1)], {1: {"protected": 1}}, [], RenderProfile(), 100, 30)
    assert not plan.regions and plan.protected[15]


def test_ema_union_always_contains_raw_box():
    t = _t(1, frames=range(0, 10))
    t.boxes = {f: (100 + f * 20, 100, 40, 50, 0.9, 0) for f in range(10)}  # 빠르게 이동
    plan = build_plan([t], {}, [], RenderProfile(pad_frames=0), 20, 30)
    for f in range(10):
        r = plan.at(f)[0]
        x = 100 + f * 20
        assert r.x1 <= x - 5 + 1e-6 and r.x2 >= x + 45 - 1e-6


def test_small_plate_fallback_x3():
    plan = build_plan([_t(1, "plate", box=(100, 100, 16, 6))], {}, [], RenderProfile(pad_ratio=1.0, pad_frames=0), 30, 30)
    r = plan.at(12)[0]
    assert r.x2 - r.x1 == pytest.approx(48)


def test_head_fallback_only_without_face():
    person = _t(5, "person", box=(100, 100, 60, 200))
    plan = build_plan([person], {}, [], RenderProfile(pad_frames=0), 30, 30)
    assert plan.at(12)[0].kind == "head"
    face = _t(1, "face", box=(115, 105, 30, 35))
    plan = build_plan([person, face], {}, [], RenderProfile(pad_frames=0), 30, 30)
    assert {r.kind for r in plan.at(12)} == {"face"}
    plan = build_plan([person], {}, [], RenderProfile(mask_head_when_no_face=False), 30, 30)
    assert not plan.regions


def test_manual_box_and_exclusions():
    rules = [{"id": 3, "kind": "manual_box", "payload": {"frames": [[0, 0, 0, 10, 10], [10, 100, 0, 10, 10]]}}]
    plan = build_plan([], {}, rules, RenderProfile(pad_ratio=1.0), 30, 30)
    assert plan.at(5)[0].x1 == pytest.approx(50)
    rules2 = [{"id": 1, "kind": "region", "payload": {"polygon": [[0, 0], [300, 0], [300, 300], [0, 300]]}},
              {"id": 2, "kind": "timerange", "payload": {"start_ms": 0, "end_ms": 100}}]
    plan = build_plan([_t(1)], {}, rules2, RenderProfile(), 100, 30)
    assert not any(plan.regions.values())
    assert plan.is_excluded(15, (110, 110, 120, 120))


def test_interpolate_keyframes():
    k = interpolate_keyframes([[0, 0, 0, 10, 10], [4, 40, 0, 10, 10]])
    assert k[2] == pytest.approx((20, 0, 10, 10))


@pytest.mark.parametrize("style", ["pixelate", "gaussian", "solid"])
def test_styles_change_pixels(style):
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (200, 200, 3), dtype=np.uint8)
    out = apply_masks(img.copy(), [Region(50, 50, 150, 150, "face")], RenderProfile(style=style))
    assert np.abs(out[60:140, 60:140].astype(int) - img[60:140, 60:140]).mean() > 20
    assert (out[:40] == img[:40]).all()


def test_pixelate_block_floor():
    """작은 얼굴(폭 16px)에도 블록은 8px 이상이어야 한다."""
    img = np.zeros((64, 64, 3), np.uint8)
    img[::2, ::2] = 255
    out = apply_masks(img.copy(), [Region(0, 0, 16, 16, "face")], RenderProfile(strength=8))
    blk = out[:MIN_BLOCK, :MIN_BLOCK]
    assert (blk == blk[0, 0]).all()


def test_profile_floor_and_segment_fallback():
    p = RenderProfile.from_dict({"style": "segment", "strength": 999, "pad_ratio": 9})
    assert p.style == "pixelate" and p.strength == 32 and p.pad_ratio == 2.0


def test_coverage():
    assert coverage((0, 0, 10, 10), [Region(0, 0, 5, 10, "face")]) == pytest.approx(0.5, abs=0.05)
