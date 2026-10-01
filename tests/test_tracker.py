import numpy as np

from worker.pipeline.track import ByteTracker, densify, iou_matrix


def _box(x, y, w=50, h=60, s=0.9):
    return [x, y, x + w, y + h, s]


def test_single_object_one_track():
    tr = ByteTracker(interval=2)
    for f in range(0, 60, 2):
        tr.update(f, np.array([_box(100 + f * 3, 100)]))
    ts = tr.flush()
    assert len(ts) == 1
    assert min(ts[0].obs) == 0 and max(ts[0].obs) == 58


def test_two_objects_keep_ids():
    tr = ByteTracker()
    ids = set()
    for f in range(30):
        m = tr.update(f, np.array([_box(100 + f, 100), _box(500 - f, 300)]))
        ids |= {t for _, t in m}
    assert len(ids) == 2


def test_low_confidence_creates_track_and_does_not_fragment():
    """안전 편차: 0.3대 저신뢰 검출도 트랙이 되고, 깜빡여도 조각나지 않는다."""
    tr = ByteTracker(interval=2)
    for f in range(0, 80, 2):
        dets = [_box(200 + f, 200, s=0.35)] if (f // 2) % 3 != 1 else []
        tr.update(f, np.array(dets).reshape(-1, 5))
    ts = tr.flush()
    assert len(ts) <= 2
    assert sum(len(t.obs) for t in ts) > 20


def test_single_detection_kept():
    tr = ByteTracker()
    tr.update(0, np.zeros((0, 5)))
    tr.update(1, np.array([_box(10, 10, s=0.31)]))
    for f in range(2, 60):
        tr.update(f, np.zeros((0, 5)))
    ts = tr.flush()
    assert len(ts) == 1 and list(ts[0].obs) == [1]


def test_densify_interpolates():
    d = densify({0: (0, 0, 10, 10, 0.9), 4: (40, 0, 50, 10, 0.8)})
    assert sorted(d) == [0, 1, 2, 3, 4]
    assert d[2][0] == 20 and d[2][5] == 1 and d[0][5] == 0


def test_iou_matrix():
    m = iou_matrix(np.array([[0, 0, 10, 10]]), np.array([[0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]]))
    assert np.allclose(m[0], [1.0, 1 / 3, 0.0])


def test_plate_distance_association_fast_motion():
    """번호판(dist_assoc): 검출 사이 IoU 0이 될 만큼 빨라도 한 트랙으로 이어진다."""
    tr = ByteTracker(interval=2, dist_assoc=True)
    for f in range(0, 40, 2):
        tr.update(f, np.array([[10 + f * 35, 500, 72 + f * 35, 515, 0.9]]))  # 프레임당 35px
    ts = tr.flush()
    assert len(ts) == 1 and len(densify(ts[0].obs)) == 39


def test_face_no_distance_association():
    tr = ByteTracker(interval=2)
    for f in range(0, 20, 2):
        tr.update(f, np.array([[10 + f * 35, 500, 72 + f * 35, 560, 0.9]]))
    assert len(tr.flush()) > 1


def test_lost_face_track_does_not_jump_to_another_person():
    """보호 안전: 사라진 사람 A의 트랙이 잠시 뒤 같은 자리를 지나는 B에게 붙지 않는다."""
    tr = ByteTracker(interval=2, max_lost_steps=3)
    ids_a, ids_b = set(), set()
    for f in range(0, 20, 2):        # A: 제자리에 있다가 f18 이후 사라짐
        ids_a |= {t for _, t in tr.update(f, np.array([[500, 300, 530, 338, 0.9]]))}
    for f in range(20, 30, 2):       # 공백(아무도 없음)
        tr.update(f, np.zeros((0, 5)))
    for f in range(30, 50, 2):       # B: 같은 자리로 들어옴
        ids_b |= {t for _, t in tr.update(f, np.array([[505, 302, 535, 340, 0.9]]))}
    assert ids_a.isdisjoint(ids_b)
