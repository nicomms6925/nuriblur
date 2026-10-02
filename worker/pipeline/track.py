"""ByteTrack 자체 포팅 (G1-04, 원 저장소 MIT — 의존성 없이 재작성).

파라미터(03 스펙): high 0.5 / low 0.1 / match 0.8 / buffer 30프레임.

NuriBlur 안전 편차 (deny-by-default):
- 원본은 score ≥ high+0.1 인 검출만 새 트랙을 만들지만, 여기서는 연관되지 않은 모든 검출
  (검출기 클래스 임계 통과분)로 새 트랙을 만든다. 0.3~0.6 얼굴도 반드시 마스킹되어야 하기 때문.
- 확정되지 못하고 사라진 트랙(1회 검출)도 버리지 않고 결과에 남긴다.
- 검출 간격 사이 프레임은 관측 박스 사이 선형 보간(interpolated=1)으로 채운다.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import scipy.linalg
from scipy.optimize import linear_sum_assignment

HIGH = 0.5
LOW = 0.1
MATCH = 0.8
BUFFER = 30


class KalmanFilter:
    """xyah 상태 칼만 필터 (ByteTrack 원본과 동일한 노이즈 모델)."""

    def __init__(self) -> None:
        ndim, dt = 4, 1.0
        self._motion_mat = np.eye(2 * ndim)
        for i in range(ndim):
            self._motion_mat[i, ndim + i] = dt
        self._update_mat = np.eye(ndim, 2 * ndim)
        self._std_pos = 1.0 / 20
        self._std_vel = 1.0 / 160

    def initiate(self, m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[m, np.zeros_like(m)]
        h = m[3]
        std = [2 * self._std_pos * h, 2 * self._std_pos * h, 1e-2, 2 * self._std_pos * h,
               10 * self._std_vel * h, 10 * self._std_vel * h, 1e-5, 10 * self._std_vel * h]
        return mean, np.diag(np.square(std))

    def predict(self, mean: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        std = [self._std_pos * h, self._std_pos * h, 1e-2, self._std_pos * h,
               self._std_vel * h, self._std_vel * h, 1e-5, self._std_vel * h]
        mean = self._motion_mat @ mean
        cov = self._motion_mat @ cov @ self._motion_mat.T + np.diag(np.square(std))
        return mean, cov

    def update(self, mean: np.ndarray, cov: np.ndarray, m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        std = [self._std_pos * h, self._std_pos * h, 1e-1, self._std_pos * h]
        pm = self._update_mat @ mean
        pc = self._update_mat @ cov @ self._update_mat.T + np.diag(np.square(std))
        chol, lower = scipy.linalg.cho_factor(pc, lower=True, check_finite=False)
        gain = scipy.linalg.cho_solve((chol, lower), (cov @ self._update_mat.T).T, check_finite=False).T
        mean = mean + (m - pm) @ gain.T
        cov = cov - gain @ pc @ gain.T
        return mean, cov


def xyxy_to_xyah(b: np.ndarray) -> np.ndarray:
    w, h = b[2] - b[0], b[3] - b[1]
    return np.array([b[0] + w / 2, b[1] + h / 2, w / max(h, 1e-6), h], dtype=np.float64)


def xyah_to_xyxy(m: np.ndarray) -> np.ndarray:
    w = m[2] * m[3]
    return np.array([m[0] - w / 2, m[1] - m[3] / 2, m[0] + w / 2, m[1] + m[3] / 2])


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    bb = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / (aa[:, None] + bb[None, :] - inter + 1e-9)


SMALL = 48        # 이보다 작은 박스(군중 속 얼굴 등)는 버퍼 IoU로 연관 (C-BIoU)
BUFFER_RATIO = 0.5


def buffered(b: np.ndarray) -> np.ndarray:
    """작은 박스를 크기의 BUFFER_RATIO 만큼 사방으로 넓힌다. 16px 얼굴이 4프레임에 8px 움직여도 이어지도록."""
    b = np.asarray(b, dtype=np.float64).reshape(-1, 4).copy()
    if len(b) == 0:
        return b
    s = np.maximum(b[:, 2] - b[:, 0], b[:, 3] - b[:, 1])
    pad = np.where(s < SMALL, s * BUFFER_RATIO, 0.0)
    b[:, 0] -= pad
    b[:, 1] -= pad
    b[:, 2] += pad
    b[:, 3] += pad
    return b


def biou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return iou_matrix(buffered(a), buffered(b))


def assign(cost: np.ndarray, thresh: float) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    if cost.size == 0:
        return [], list(range(cost.shape[0])), list(range(cost.shape[1]))
    c = cost.copy()
    c[c > thresh] = thresh + 1e4
    r, k = linear_sum_assignment(c)
    matches = [(i, j) for i, j in zip(r, k, strict=True) if cost[i, j] <= thresh]
    mi = {i for i, _ in matches}
    mj = {j for _, j in matches}
    return matches, [i for i in range(cost.shape[0]) if i not in mi], [j for j in range(cost.shape[1]) if j not in mj]


TRACKED, LOST, REMOVED = 0, 1, 2


@dataclass(eq=False)
class STrack:
    tid: int
    mean: np.ndarray
    cov: np.ndarray
    score: float
    start_frame: int
    end_frame: int
    state: int = TRACKED
    activated: bool = False
    hits: int = 1
    # frame -> (x1, y1, x2, y2, conf)  관측 박스만
    obs: dict[int, tuple[float, float, float, float, float]] = field(default_factory=dict)

    @property
    def xyxy(self) -> np.ndarray:
        return xyah_to_xyxy(self.mean[:4])


class ByteTracker:
    def __init__(self, interval: int = 1, fps: float = 30.0, id_alloc: Callable[[], int] | None = None,
                 dist_assoc: bool = False, max_lost_steps: int | None = None):
        self.kf = KalmanFilter()
        # 번호판 전용: 빠르게 지나가는 작은 박스는 검출 사이에 IoU가 0이 되므로 중심 거리로 한 번 더 잇는다.
        # 얼굴에는 쓰지 않는다(다른 사람 얼굴이 보호 트랙에 붙으면 노출됨).
        self.dist_assoc = dist_assoc
        self.interval = max(1, interval)
        self.max_lost = max(1, int(round(BUFFER * (fps / 30.0))))
        # 보호 안전: 얼굴·전신 트랙은 놓친 뒤 오래 기다리지 않는다 — 오래 기다리면 같은 자리를 지나는
        # 다른 사람에게 붙어, 그 사람을 클릭해 보호할 때 앞사람 구간까지 보호(노출)된다.
        if max_lost_steps is not None:
            self.max_lost = min(self.max_lost, max_lost_steps * max(1, interval))
        self.tracked: list[STrack] = []
        self.lost: list[STrack] = []
        self.finished: list[STrack] = []
        self._alloc = id_alloc or itertools.count(1).__next__

    def _new_id(self) -> int:
        return self._alloc()

    def active(self) -> list[STrack]:
        return self.finished + self.tracked + self.lost

    def _observe(self, t: STrack, frame: int, box: np.ndarray, score: float) -> None:
        t.mean, t.cov = self.kf.update(t.mean, t.cov, xyxy_to_xyah(box))
        t.score = score
        t.end_frame = frame
        t.state = TRACKED
        t.hits += 1
        t.activated = True
        t.obs[frame] = (float(box[0]), float(box[1]), float(box[2]), float(box[3]), float(score))

    def update(self, frame: int, dets: np.ndarray) -> list[tuple[int, int]]:
        """dets: N×5 [x1,y1,x2,y2,score]. 반환: [(det_index, track_id)] 이번 프레임 관측 매칭."""
        dets = np.asarray(dets, dtype=np.float64).reshape(-1, 5)
        result: list[tuple[int, int]] = []
        for t in self.tracked + self.lost:
            t.mean, t.cov = self.kf.predict(t.mean, t.cov)

        hi_idx = np.where(dets[:, 4] >= HIGH)[0]
        lo_idx = np.where((dets[:, 4] >= LOW) & (dets[:, 4] < HIGH))[0]
        confirmed = [t for t in self.tracked if t.activated]
        unconfirmed = [t for t in self.tracked if not t.activated]
        lost_c = [t for t in self.lost if t.activated]
        lost_u = [t for t in self.lost if not t.activated]
        pool = confirmed + lost_c

        # 1차: high 검출 ↔ (확정 추적 + 확정 잃음), IoU × score 융합.
        # 버퍼 IoU는 직전 단계에 이어진(TRACKED) 트랙에만 — 잃은 트랙 재연결은 일반 IoU(다른 사람 오연결 방지)
        tb = np.array([t.xyxy for t in pool]).reshape(-1, 4)
        iou = biou(tb, dets[hi_idx, :4])
        if len(lost_c) and iou.size:
            iou[len(confirmed):] = iou_matrix(tb[len(confirmed):], dets[hi_idx, :4])
        cost = 1 - iou * dets[hi_idx, 4][None, :] if iou.size else 1 - iou
        m, u_trk, u_det = assign(cost, MATCH)
        for i, j in m:
            d = hi_idx[j]
            self._observe(pool[i], frame, dets[d, :4], dets[d, 4])
            result.append((int(d), pool[i].tid))
        rem_hi = [hi_idx[j] for j in u_det]

        # 2차: low 검출 ↔ 남은 확정 TRACKED 트랙
        r_tracks = [pool[i] for i in u_trk if pool[i].state == TRACKED]
        tb = np.array([t.xyxy for t in r_tracks]).reshape(-1, 4)
        m2, u_trk2, u_det2 = assign(1 - biou(tb, dets[lo_idx, :4]), 0.5)
        for i, j in m2:
            d = lo_idx[j]
            self._observe(r_tracks[i], frame, dets[d, :4], dets[d, 4])
            result.append((int(d), r_tracks[i].tid))
        rem_lo = [lo_idx[j] for j in u_det2]
        for i in u_trk2:
            r_tracks[i].state = LOST
        r_lost = [pool[i] for i in u_trk if pool[i].state == LOST]

        # 3차(안전 편차): 미확정 트랙 + 남은 잃은 트랙 ↔ 남은 검출(high+low).
        # 저신뢰 얼굴이 깜빡여도 트랙이 조각나지 않게 한다. 2회 관측 시 확정.
        cand = unconfirmed + lost_u + r_lost
        tb = np.array([t.xyxy for t in cand]).reshape(-1, 4)
        rem_arr = np.array(rem_hi + rem_lo, dtype=int)
        iou = biou(tb, dets[rem_arr, :4]) if len(rem_arr) else np.zeros((len(cand), 0))
        n_fresh = len(unconfirmed)  # 미확정(직전 단계 생성)만 버퍼 IoU, 잃은 트랙은 일반 IoU
        if iou.size and len(cand) > n_fresh:
            iou[n_fresh:] = iou_matrix(tb[n_fresh:], dets[rem_arr, :4])
        m3, u_c, u_det3 = assign(1 - iou, 0.7)
        for i, j in m3:
            d = rem_arr[j]
            self._observe(cand[i], frame, dets[d, :4], dets[d, 4])
            result.append((int(d), cand[i].tid))
        for i in u_c:
            if cand[i].state == TRACKED:
                cand[i].state = LOST
        rem = [rem_arr[j] for j in u_det3]

        # 3.5차(번호판 전용): 남은 검출 ↔ 이번에 못 이은 트랙, 중심 거리 + 크기 비
        if self.dist_assoc and rem:
            cand2 = [cand[i] for i in u_c] + [t for t in self.lost if t.state == LOST and t not in cand]
            cand2 = [t for t in cand2 if frame - t.end_frame <= 3 * self.interval]
            if cand2:
                cost = np.full((len(cand2), len(rem)), 1e6)
                for i, t in enumerate(cand2):
                    tb = t.obs[t.end_frame]
                    tw, th = tb[2] - tb[0], tb[3] - tb[1]
                    tcx, tcy = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
                    gap = frame - t.end_frame
                    # 관측이 2개 이상이면 속도로 예측한 위치에서 잰다 — 이동 방향과 어긋난 오검출(예: 화면 밖으로
                    # 나가는 번호판 위쪽의 차체)이 트랙에 붙어 마스크가 번호판에서 벗어나는 것을 막는다
                    prev = [f for f in t.obs if f < t.end_frame]
                    if prev:
                        pf = max(prev)
                        pb = t.obs[pf]
                        vx = (tcx - (pb[0] + pb[2]) / 2) / (t.end_frame - pf)
                        vy = (tcy - (pb[1] + pb[3]) / 2) / (t.end_frame - pf)
                        px, py = tcx + vx * gap, tcy + vy * gap
                        limit = 1.5 * max(tw, th) + 0.5 * np.hypot(vx, vy) * gap
                    else:
                        px, py = tcx, tcy
                        limit = 2.5 * max(tw, th) * gap / self.interval
                    for j, d in enumerate(rem):
                        w, h = dets[d, 2] - dets[d, 0], dets[d, 3] - dets[d, 1]
                        if not (0.5 <= w / max(tw, 1e-6) <= 2.0 and 0.5 <= h / max(th, 1e-6) <= 2.0):
                            continue
                        dist = np.hypot((dets[d, 0] + dets[d, 2]) / 2 - px, (dets[d, 1] + dets[d, 3]) / 2 - py)
                        if dist <= limit:
                            cost[i, j] = dist
                r_i, c_j = linear_sum_assignment(cost)
                used = set()
                for i, j in zip(r_i, c_j, strict=True):
                    if cost[i, j] < 1e6:
                        d = rem[j]
                        self._observe(cand2[i], frame, dets[d, :4], dets[d, 4])
                        result.append((int(d), cand2[i].tid))
                        used.add(d)
                rem = [d for d in rem if d not in used]

        # 4차(안전 편차): 남은 모든 검출로 새 트랙
        for d in rem:
            box = dets[d, :4]
            mean, cov = self.kf.initiate(xyxy_to_xyah(box))
            t = STrack(self._new_id(), mean, cov, float(dets[d, 4]), frame, frame,
                       activated=(frame == 0) or dets[d, 4] >= HIGH + 0.1)
            t.obs[frame] = (float(box[0]), float(box[1]), float(box[2]), float(box[3]), float(dets[d, 4]))
            self.tracked.append(t)
            result.append((int(d), t.tid))

        # 상태 정리 — 미확정 트랙은 짧게(검출 3회분)만 기다린다
        short = min(self.max_lost, 3 * self.interval)
        alive, lost = [], []
        for t in self.tracked + self.lost:
            if t.state == TRACKED:
                alive.append(t)
            elif frame - t.end_frame > (self.max_lost if t.activated else short):
                t.state = REMOVED
                self.finished.append(t)
            else:
                lost.append(t)
        self.tracked, self.lost = alive, lost
        return result

    def flush(self) -> list[STrack]:
        """남은 트랙까지 모두 종료하고 전체 결과를 돌려준다."""
        out = self.finished + self.tracked + self.lost
        self.finished, self.tracked, self.lost = [], [], []
        return sorted(out, key=lambda t: t.tid)


def densify(obs: dict[int, tuple[float, float, float, float, float]]
            ) -> dict[int, tuple[float, float, float, float, float, int]]:
    """관측 박스(xyxy) → 모든 프레임 (x,y,w,h,conf,interpolated). 관측 사이를 선형 보간."""
    frames = sorted(obs)
    out: dict[int, tuple[float, float, float, float, float, int]] = {}
    for a, b in zip(frames, frames[1:] + [None], strict=True):
        x1, y1, x2, y2, c = obs[a]
        out[a] = (x1, y1, x2 - x1, y2 - y1, c, 0)
        if b is None or b - a <= 1:
            continue
        bx1, by1, bx2, by2, bc = obs[b]
        for f in range(a + 1, b):
            t = (f - a) / (b - a)
            ix1 = x1 + (bx1 - x1) * t
            iy1 = y1 + (by1 - y1) * t
            ix2 = x2 + (bx2 - x2) * t
            iy2 = y2 + (by2 - y2) * t
            out[f] = (ix1, iy1, ix2 - ix1, iy2 - iy1, min(c, bc), 1)
    return out
