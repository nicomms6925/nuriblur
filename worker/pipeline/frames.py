"""GetFrame — 검수 스크러빙용 단일 프레임(원본/마스킹).

분석 때 저장한 frame_pts 표로 키프레임 seek 후 목표 PTS까지 디코딩한다.
열린 컨테이너는 경로별로 캐시해 연속 스크러빙을 빠르게 한다.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

import av
import cv2
import numpy as np

from worker.errors import CodecError
from worker.pipeline.decode import upright


class FrameReader:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.c = av.open(self.path)
        self.vs = self.c.streams.video[0]
        self.lock = threading.Lock()
        self.last: tuple[int, np.ndarray] | None = None
        self.pos_pts: int | None = None
        self._gen = None

    def _decode_from(self, pts: int | None):
        if pts is not None:
            self.c.seek(pts, stream=self.vs, backward=True, any_frame=False)
        else:
            self.c.seek(0)
        return self.c.decode(self.vs)

    def get(self, pts: int | None, index: int) -> np.ndarray:
        with self.lock:
            # 바로 다음 프레임이면 이어서 디코딩 (재생/한 프레임 앞으로)
            if pts is None:
                gen = self._decode_from(None)
                for i, fr in enumerate(gen):
                    if i == index:
                        return upright(fr.to_ndarray(format="bgr24"), int(round(fr.rotation or 0)) % 360)
                raise CodecError(f"프레임 {index}를 찾을 수 없습니다")
            if self._gen is None or self.pos_pts is None or pts <= self.pos_pts or pts - self.pos_pts > 30 * 3000:
                self._gen = self._decode_from(pts)
            for fr in self._gen:
                self.pos_pts = fr.pts
                if fr.pts is not None and fr.pts >= pts:
                    return upright(fr.to_ndarray(format="bgr24"), int(round(fr.rotation or 0)) % 360)
            self._gen = None
            raise CodecError(f"프레임 {index}(pts {pts})를 찾을 수 없습니다")

    def close(self) -> None:
        self.c.close()


class ReaderCache:
    def __init__(self, size: int = 4):
        self.size = size
        self.items: OrderedDict[str, FrameReader] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, path: str | Path) -> FrameReader:
        key = str(path)
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
            r = FrameReader(key)
            self.items[key] = r
            while len(self.items) > self.size:
                _, old = self.items.popitem(last=False)
                old.close()
            return r

    def clear(self) -> None:
        with self.lock:
            for r in self.items.values():
                r.close()
            self.items.clear()


def to_jpeg(img: np.ndarray, max_width: int = 0, quality: int = 85) -> bytes:
    if max_width and img.shape[1] > max_width:
        s = max_width / img.shape[1]
        img = cv2.resize(img, (max_width, int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""
