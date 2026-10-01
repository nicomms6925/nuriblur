"""encode (G1-09) — PyAV 인코더, 오디오 패스스루, 메타데이터 제거.

인코더 우선순위: NVENC → QSV → AMF → Media Foundation(OS 내장) → libopenh264 → mpeg4.
**GPL 인코더(libx264, libx265 등)는 절대 쓰지 않는다** (docs/09).
일부 HW 인코더는 드라이버 문제로 프로세스를 죽일 수 있으므로, 사용 가능 여부는 서브프로세스에서
한 번 시험하고 결과를 캐시한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import numpy as np

from worker.errors import EncoderError

GPL_ENCODERS = {"libx264", "libx264rgb", "libx265", "libxvid", "libxavs", "libxavs2", "libvidstab"}
CANDIDATES = {
    "h264": ["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libopenh264", "mpeg4"],
    "hevc": ["hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_mf", "libkvazaar"],  # kvazaar: BSD-3
}
NV12_ENCODERS = ("_mf", "_qsv")
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}


def _cache_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "NuriBlur"
    base.mkdir(parents=True, exist_ok=True)
    return base / "encoders.json"


def _probe_one(name: str, timeout: float = 45.0) -> bool:
    if name in GPL_ENCODERS:
        return False
    try:
        av.codec.Codec(name, "w")
    except Exception:  # noqa: BLE001
        return False
    try:
        cmd = ([sys.executable, "--probe-encoder", name] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "worker.pipeline.encode", "--probe", name])
        r = subprocess.run(cmd,
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(Path(__file__).resolve().parents[2]),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0 and "PROBE_OK" in r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def working_encoders(refresh: bool = False) -> dict[str, bool]:
    key = f"av{av.__version__}"
    cp = _cache_path()
    cache: dict[str, Any] = {}
    if cp.exists() and not refresh:
        try:
            cache = json.loads(cp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    res = cache.get(key, {})
    changed = False
    for names in CANDIDATES.values():
        for n in names:
            if n not in res:
                res[n] = _probe_one(n)
                changed = True
    if changed:
        cache[key] = res
        try:
            cp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        except OSError:
            pass
    return res


def select_encoder(codec: str, src_codec: str, warn: Callable[[str, str], None] | None = None) -> str:
    want = codec if codec in CANDIDATES else ("hevc" if src_codec in ("hevc", "h265") else "h264")
    ok = working_encoders()
    for n in CANDIDATES[want]:
        if ok.get(n):
            return n
    if want == "hevc":
        if warn:
            warn("W_ENCODER_FALLBACK", "HEVC 인코더를 쓸 수 없어 H.264로 출력합니다")
        for n in CANDIDATES["h264"]:
            if ok.get(n):
                return n
    raise EncoderError("사용 가능한 (LGPL 호환) 영상 인코더가 없습니다")


def target_bitrate(media: dict[str, Any], quality: str) -> int:
    br = int(media.get("bit_rate") or 0)
    if br <= 0:
        try:
            size = Path(media["path"]).stat().st_size
            dur = max((media.get("duration_ms") or 0) / 1000, 0.1)
            br = int(size * 8 / dur * 0.95)
        except OSError:
            br = 0
    if br <= 0:
        br = int(8_000_000 * (media["width"] * media["height"]) / (1920 * 1080))
    mult = {"source": 1.0, "high": 1.5, "normal": 0.7}.get(quality, 1.0)
    return int(min(br * mult, br * 1.5))


class VideoWriter:
    """출력 영상 쓰기. 원본의 오디오 패킷을 시간순으로 끼워 넣는다(재인코딩 없음)."""

    def __init__(self, out_path: str | Path, media: dict[str, Any], codec: str = "source", quality: str = "source",
                 strip_meta: bool = True, keep_audio: bool = True, warn: Callable[[str, str], None] | None = None):
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.media = media
        self.warn = warn
        self.encoder = select_encoder(codec, media.get("codec", ""), warn)
        self.w = int(media["width"]) // 2 * 2
        self.h = int(media["height"]) // 2 * 2
        self.src = av.open(str(media["path"]))
        sv = self.src.streams.video[0]
        self.src_tb: Fraction = sv.time_base or Fraction(1, 90000)
        fps = Fraction(media.get("fps") or 30).limit_denominator(1001)
        try:
            self.out = av.open(str(self.out_path), "w")
        except av.FFmpegError as e:
            raise EncoderError(f"출력 파일을 만들 수 없습니다: {e}") from e
        if not strip_meta:
            self.out.metadata.update(self.src.metadata)
        self.vs = self.out.add_stream(self.encoder, rate=fps)
        self.vs.width, self.vs.height = self.w, self.h
        self.vs.pix_fmt = "nv12" if self.encoder.endswith(NV12_ENCODERS) else "yuv420p"
        self.vs.bit_rate = target_bitrate(media, quality)
        self.vs.time_base = self.src_tb
        self.vs.codec_context.time_base = self.src_tb
        if self.encoder.endswith("_nvenc"):
            self.vs.options = {"preset": "p4", "rc": "vbr"}
        # 오디오
        self.aud_in = None
        self.aud_out = None
        self.aud_iter = None
        self.aud_next = None
        self.aud_dec = None
        self.aud_enc = None
        if keep_audio and self.src.streams.audio:
            ia = self.src.streams.audio[0]
            ext = self.out_path.suffix.lower()
            copy_ok = ext in (".mkv",) or ia.codec_context.name in MP4_AUDIO_OK or ext == ".mov"
            self.aud_in = av.open(str(media["path"]))
            ain = self.aud_in.streams.audio[0]
            if copy_ok:
                self.aud_out = self.out.add_stream_from_template(ain)
                self.aud_iter = (p for p in self.aud_in.demux(ain) if p.dts is not None and p.size > 0)
            else:
                if warn:
                    warn("W_AUDIO_TRANSCODE", f"{ia.codec_context.name} 오디오는 이 컨테이너에 복사할 수 없어 AAC로 변환합니다")
                self.aud_out = self.out.add_stream("aac", rate=ain.codec_context.sample_rate or 48000)
                self.aud_iter = self._transcode_audio(ain)
            self._advance_audio()
        self.n = 0
        self.last_pts: int | None = None

    def _transcode_audio(self, ain):
        for frame in self.aud_in.decode(ain):  # type: ignore[union-attr]
            frame.pts = None
            yield from self.aud_out.encode(frame)  # type: ignore[union-attr]
        yield from self.aud_out.encode(None)  # type: ignore[union-attr]

    def _advance_audio(self) -> None:
        try:
            self.aud_next = next(self.aud_iter) if self.aud_iter is not None else None
        except StopIteration:
            self.aud_next = None

    def _audio_until(self, t_sec: float | None) -> None:
        while self.aud_next is not None:
            p = self.aud_next
            pt = float(p.dts * p.time_base) if p.dts is not None and p.time_base else None
            if t_sec is not None and pt is not None and pt > t_sec:
                return
            p.stream = self.aud_out
            try:
                self.out.mux(p)
            except av.FFmpegError:
                pass
            self._advance_audio()

    def write(self, bgr: np.ndarray, pts: int | None) -> None:
        if bgr.shape[0] != self.h or bgr.shape[1] != self.w:
            bgr = bgr[: self.h, : self.w]
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(bgr), format="bgr24").reformat(format=self.vs.pix_fmt)
        if pts is None or (self.last_pts is not None and pts <= self.last_pts):
            pts = (self.last_pts + 1) if self.last_pts is not None else 0
        frame.pts = pts
        frame.time_base = self.src_tb
        self.last_pts = pts
        try:
            for p in self.vs.encode(frame):
                self.out.mux(p)
        except av.FFmpegError as e:
            raise EncoderError(f"인코딩 오류({self.encoder}): {e}") from e
        self.n += 1
        if self.aud_out is not None:
            self._audio_until(float(pts * self.src_tb))

    def close(self) -> None:
        try:
            for p in self.vs.encode(None):
                self.out.mux(p)
            if self.aud_out is not None:
                self._audio_until(None)
        finally:
            self.out.close()
            self.src.close()
            if self.aud_in is not None:
                self.aud_in.close()

    def abort(self) -> None:
        try:
            self.out.close()
        except Exception:  # noqa: BLE001
            pass
        self.src.close()
        if self.aud_in is not None:
            self.aud_in.close()
        self.out_path.unlink(missing_ok=True)


def _probe_main(name: str) -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = av.open(os.path.join(d, "p.mp4"), "w")
        s = c.add_stream(name, rate=30)
        s.width, s.height = 320, 240
        s.pix_fmt = "nv12" if name.endswith(NV12_ENCODERS) else "yuv420p"
        s.bit_rate = 1_000_000
        n = 0
        for i in range(10):
            a = np.full((240, 320, 3), i * 20, np.uint8)
            f = av.VideoFrame.from_ndarray(a, format="bgr24").reformat(format=s.pix_fmt)
            f.pts = i
            for p in s.encode(f):
                c.mux(p)
                n += 1
        for p in s.encode(None):
            c.mux(p)
            n += 1
        c.close()
    if n:
        print("PROBE_OK")
        return 0
    return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        sys.exit(_probe_main(sys.argv[2]))
