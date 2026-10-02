import zipfile

from worker.io.project import Project, TrackRow


def test_roundtrip_and_zip_layout(tmp_path):
    path = tmp_path / "a.nbproj"
    p = Project.create(path)
    p.set_media({"path": "C:/v.mp4", "sha256": "ab" * 32, "codec": "h264", "width": 10, "height": 10, "fps": 30.0,
                 "is_vfr": 0, "frames": 3, "duration_ms": 100, "rotation": 0, "audio_codec": ""})
    t = TrackRow(id=1, cls="face", start_f=0, end_f=2)
    t.boxes = {0: (1, 2, 3, 4, 0.9, 0), 1: (1, 2, 3, 4, 0.8, 1)}
    p.write_tracks([t])
    p.write_thumb(1, b"\xff\xd8jpeg")
    p.replace_rules([{"kind": "click", "payload": {"track_id": 1}}], actor="tester")
    p.add_frame_pts([(0, 0), (1, 512), (2, 1024)])
    p.save({"m": "x"})
    workdir = p.workdir
    p.close()
    assert not workdir.exists(), "작업 폴더는 닫을 때 삭제되어야 한다"
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
    assert {"project.sqlite", "manifest.json", "thumbs/1.jpg"} <= names
    assert not any(n.endswith(".mp4") for n in names), "원본 영상은 포함하지 않는다"
    with Project.open(path) as q:
        assert q.media()["sha256"] == "ab" * 32
        tr = q.track(1)
        assert tr.boxes[1][5] == 1
        assert q.rules()[0]["created_by"] == "tester"
        assert q.frame_pts(2) == 1024 and q.frame_count() == 3
        assert q.read_thumb(1).startswith(b"\xff\xd8")


def test_checkpoint_truncate(tmp_path):
    p = Project.create(tmp_path / "b.nbproj")
    a = TrackRow(id=1, cls="face", start_f=0, end_f=9)
    a.boxes = {f: (0, 0, 1, 1, 1, 0) for f in range(10)}
    b = TrackRow(id=2, cls="face", start_f=6, end_f=9)
    b.boxes = {f: (0, 0, 1, 1, 1, 0) for f in range(6, 10)}
    p.write_tracks([a, b])
    p.delete_tracks_from(5)
    ts = {t.id: t for t in p.tracks()}
    assert set(ts) == {1} and ts[1].end_f == 4
    assert p.next_track_id() == 2
    p.close()
