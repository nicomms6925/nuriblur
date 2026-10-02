"""probe (G1-01) — PyAV로 미디어 정보 조회.

ffprobe 바이너리 없이 동작한다. VFR은 PTS 간격의 변동으로 판정하고,
회전은 display matrix(frame.rotation, 반시계 각도)로 읽는다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import av
import numpy as np

from worker.errors import CodecError

VFR_SAMPLE = 300


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: str | Path, with_hash: bool = True) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise CodecError(f"파일이 없습니다: {path}")
    try:
        c = av.open(str(path))
    except av.FFmpegError as e:
        raise CodecError(f"열 수 없는 미디어: {path.name} ({e})") from e
    try:
        if not c.streams.video:
            raise CodecError(f"영상 스트림이 없습니다: {path.name}")
        vs = c.streams.video[0]
        cc = vs.codec_context
        rate = vs.average_rate or vs.guessed_rate or vs.base_rate
        fps = float(rate) if rate else 0.0
        audio = c.streams.audio[0].codec_context.name if c.streams.audio else ""

        # 패킷만 훑어 프레임 수·PTS 간격 수집(디코딩 없음 → 빠름)
        pts_list: list[int] = []
        n_packets = 0
        for pkt in c.demux(vs):
            if pkt.size == 0:
                continue
            n_packets += 1
            if pkt.pts is not None and len(pts_list) < VFR_SAMPLE:
                pts_list.append(pkt.pts)
        frames = vs.frames or n_packets
        is_vfr = False
        if len(pts_list) > 10:
            d = np.diff(np.sort(np.array(pts_list, dtype=np.float64)))
            d = d[d > 0]
            if len(d) > 5 and np.median(d) > 0:
                rel = np.abs(d - np.median(d)) / np.median(d)
                is_vfr = bool((rel > 0.1).mean() > 0.05)

        # 회전: 첫 프레임 디코딩
        c.seek(0)
        rotation = 0
        for fr in c.decode(vs):
            rotation = int(round(fr.rotation or 0)) % 360
            break
        if c.duration:
            duration_ms = int(c.duration / 1000)
        elif vs.duration and vs.time_base:
            duration_ms = int(float(vs.duration * vs.time_base) * 1000)
        else:
            duration_ms = int(frames / fps * 1000) if fps else 0
        w, h = cc.width, cc.height
        if rotation in (90, 270):
            w, h = h, w
        return {
            "path": str(path.resolve()),
            "sha256": sha256_file(path) if with_hash else "",
            "codec": cc.name,
            "pix_fmt": cc.pix_fmt or "",
            "width": int(w),
            "height": int(h),
            "fps": fps,
            "is_vfr": int(is_vfr),
            "frames": int(frames),
            "duration_ms": duration_ms,
            "rotation": rotation,
            "audio_codec": audio,
            "bit_rate": int(cc.bit_rate or c.bit_rate or 0),
        }
    finally:
        c.close()
