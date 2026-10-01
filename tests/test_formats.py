"""G1-13 포맷 매트릭스: 컨테이너 × 코덱 × 회전 × VFR → probe · 분석 · 렌더 왕복."""
from fractions import Fraction

import av
import numpy as np
import pytest

from worker.jobs import JobControl
from worker.pipeline.analyze import analyze
from worker.pipeline.decode import iter_frames
from worker.pipeline.encode import GPL_ENCODERS, select_encoder, working_encoders
from worker.pipeline.probe import probe
from worker.pipeline.render import render

pytestmark = pytest.mark.slow


def _write(path, codec, w=320, h=240, n=24, rotation=0, vfr=False, audio=False):
    c = av.open(str(path), "w")
    s = c.add_stream(codec, rate=24)
    s.width, s.height = w, h
    s.pix_fmt = "nv12" if codec.endswith("_mf") else "yuv420p"
    s.bit_rate = 1_000_000  # Media Foundation 인코더는 비트레이트가 없으면 열리지 않는다
    if rotation:
        s.set_display_rotation(rotation)
    a = None
    if audio:
        a = c.add_stream("aac", rate=48000)
    pts = 0
    for i in range(n):
        img = np.full((h, w, 3), 40, np.uint8)
        img[:, : (i * 7) % w] = 200
        img[10:40, 10:60] = (0, 0, 255)  # 방향 확인용 빨간 사각형(좌상단)
        f = av.VideoFrame.from_ndarray(img, format="bgr24").reformat(format=s.pix_fmt)
        f.pts = pts
        f.time_base = Fraction(1, 24 * 100)
        pts += 100 if not vfr else (100 if i % 3 else 250)
        for p in s.encode(f):
            c.mux(p)
    for p in s.encode(None):
        c.mux(p)
    if a is not None:
        fr = av.AudioFrame.from_ndarray(np.zeros((2, 1024 * 30), np.float32), format="fltp", layout="stereo")
        fr.sample_rate = 48000
        for p in a.encode(fr):
            c.mux(p)
        for p in a.encode(None):
            c.mux(p)
    c.close()


def _h264():
    return select_encoder("h264", "h264")


@pytest.mark.parametrize("ext", [".mp4", ".mkv", ".mov"])
@pytest.mark.parametrize("codec", ["h264", "mpeg4"])
def test_container_codec_roundtrip(tmp_path, ext, codec):
    enc = _h264() if codec == "h264" else "mpeg4"
    src = tmp_path / f"in{ext}"
    _write(src, enc, audio=True)
    m = probe(src)
    assert (m["width"], m["height"]) == (320, 240) and m["frames"] == 24
    proj = tmp_path / "p.nbproj"
    analyze(src, proj, "a", JobControl("a"), lambda e: None, profile="cpu")
    out = tmp_path / f"out{ext}"
    r = render(proj, out, {"keep_audio": True}, "r", JobControl("r"), lambda e: None, run_audit=False)
    mo = probe(out)
    assert mo["frames"] == 24 and r["frames"] == 24
    assert mo["audio_codec"], "오디오가 유지되어야 한다"


def test_rotation_applied(tmp_path):
    src = tmp_path / "rot.mp4"
    _write(src, _h264(), w=320, h=240, rotation=90)
    m = probe(src)
    assert m["rotation"] == 90 and (m["width"], m["height"]) == (240, 320)
    f = next(iter_frames(src))
    assert f.bgr.shape[:2] == (320, 240)
    proj = tmp_path / "p.nbproj"
    analyze(src, proj, "a", JobControl("a"), lambda e: None, profile="cpu")
    out = tmp_path / "o.mp4"
    render(proj, out, {}, "r", JobControl("r"), lambda e: None, run_audit=False)
    mo = probe(out)
    assert (mo["width"], mo["height"]) == (240, 320) and mo["rotation"] == 0


def test_vfr_detected_and_timestamps_preserved(tmp_path):
    src = tmp_path / "vfr.mkv"
    _write(src, _h264(), vfr=True, n=30)
    assert probe(src)["is_vfr"] == 1
    proj = tmp_path / "p.nbproj"
    analyze(src, proj, "a", JobControl("a"), lambda e: None, profile="cpu")
    out = tmp_path / "o.mkv"
    render(proj, out, {}, "r", JobControl("r"), lambda e: None, run_audit=False)
    ts_in = [f.pts for f in iter_frames(src)]
    ts_out = [f.pts for f in iter_frames(out)]
    assert len(ts_in) == len(ts_out)
    din = np.diff(ts_in) / np.diff(ts_in).min()
    dout = np.diff(ts_out) / np.diff(ts_out).min()
    assert np.allclose(din, dout, atol=0.1)


def test_metadata_stripped(tmp_path):
    src = tmp_path / "meta.mp4"
    c = av.open(str(src), "w")
    c.metadata["location"] = "+37.5665+126.9780/"
    c.metadata["creation_time"] = "2026-09-28T10:00:00Z"
    s = c.add_stream(_h264(), rate=24)
    s.width, s.height, s.pix_fmt = 160, 120, "nv12" if _h264().endswith("_mf") else "yuv420p"
    s.bit_rate = 500_000
    for i in range(5):
        f = av.VideoFrame.from_ndarray(np.zeros((120, 160, 3), np.uint8), format="bgr24").reformat(format=s.pix_fmt)
        f.pts = i
        for p in s.encode(f):
            c.mux(p)
    for p in s.encode(None):
        c.mux(p)
    c.close()
    proj = tmp_path / "p.nbproj"
    analyze(src, proj, "a", JobControl("a"), lambda e: None, profile="cpu")
    out = tmp_path / "o.mp4"
    render(proj, out, {"strip_meta": True}, "r", JobControl("r"), lambda e: None, run_audit=False)
    with av.open(str(out)) as o:
        assert "location" not in o.metadata and "creation_time" not in o.metadata


def test_no_gpl_encoder_selected():
    ok = working_encoders()
    assert not any(ok.get(n) for n in GPL_ENCODERS)
    assert _h264() not in GPL_ENCODERS
